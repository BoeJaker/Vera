/**
 * vera-graph.js — Unified reusable graph element for Vera panels
 * ============================================================================
 * One graph implementation, used across Discover/Live Crawl, Datasets,
 * Entities, Main Fabric Graph, and any other panel that needs to render
 * nodes and edges.
 *
 * v2 — context-aware actions
 * --------------------------
 * Every graph instance now ships a built-in action menu that is driven by
 * a server-side registry (`/fabric/graph/node_actions?node_label=…`).
 * When the user clicks any node:
 *
 *   1. The detail drawer opens.
 *   2. The drawer queries the registry for actions applicable to the
 *      clicked node's label (Dataset, Source, Entity, FabricRecord, …).
 *   3. Each action renders with its declared options (configurable inline:
 *      bool / int / float / select / string).
 *   4. Hitting Run posts to /fabric/graph/run_node_action and opens a
 *      live output strip (collapsible) that streams the action's declared
 *      progress event (e.g. fabric.entity_graph.progress) via the shared
 *      WebSocket. Newly-emitted entities / records / edges are added to
 *      the graph live as they appear.
 *   5. When the action completes the graph re-fetches its current
 *      snapshot so any persisted side-effects show up.
 *
 * The host panel does not need to wire any of this up — passing
 * `actionsEnabled: true` in the graph options is enough. A panel that
 * wants to override an action's behaviour locally can still pass
 * `onAction(action_id, node, instance)` and return `false` to suppress
 * the default server roundtrip.
 *
 * Event stream subscription paths (in order of preference):
 *   - opts.eventBus  — function passed by the host that takes (typePrefix, cb)
 *                       and returns an unsubscribe function. Used when the
 *                       host already maintains a WS or SSE bridge.
 *   - parent harness — `vera_fabric_event` postMessages from window.parent.
 *   - direct WS     — opens its own /ws/mcp connection and subscribes to
 *                       events.
 *
 * Backend contract:
 *   GET  /fabric/graphs/snapshot?graph=fabric&dataset_id=X
 *   GET  /fabric/entity_graph/snapshot?dataset_id=X&include_datasets=1&include_records=1
 *   GET  /fabric/graph/node_actions?node_label=Dataset&node_id=X
 *   POST /fabric/graph/run_node_action  body: {node_label, node_id, action_id, options}
 *
 * Theming: uses the existing Vera CSS variables (--bg0/--bg1/--bg2,
 * --acc, --acc2, --acc3, --err, --ok, --text, --dim, --dim2, --border,
 * --radius, --mono).
 */
(function(){
  'use strict';

  // ─── Modular sidebar plugin registry ─────────────────────────────────────
  // Companion files (e.g. vera_graph_panel_loom.js, vera_graph_panel_discover.js,
  // vera_graph_panel_table.js) register left-side sidebar panels here. Every
  // graph created via veraUI.Graph.create() picks up the registered panels
  // automatically, so loading a companion file once enables it everywhere the
  // graph is embedded.
  //
  // A panel definition:
  //   {
  //     id:     'loom',                // unique key
  //     title:  'Loom',                // shown in the panel header
  //     icon:   '\u29d6',             // single glyph for the rail tab
  //     order:  10,                    // sort order in the rail (optional)
  //     mount:  function(bodyEl, graph, panelApi){ ... },  // build the UI
  //     unmount:function(bodyEl, graph){ ... },            // optional cleanup
  //   }
  // `mount` receives: the panel body element to populate, the live graph
  // instance (so it can call graph.load(), graph.fetchSnapshot(), etc.), and a
  // small panelApi ({ activate, isActive, graphContainer, apiBase }).
  var _PANEL_REGISTRY = [];          // [{id,title,icon,order,mount,unmount}]
  var _LIVE_GRAPHS    = [];          // graph instances currently on the page

  function registerPanel(def){
    if (!def || !def.id || typeof def.mount !== 'function') {
      if (typeof console !== 'undefined') console.warn('veraUI.Graph.registerPanel: invalid panel def', def);
      return;
    }
    // Replace an existing registration with the same id (hot-reload friendly)
    var idx = -1;
    for (var i = 0; i < _PANEL_REGISTRY.length; i++) {
      if (_PANEL_REGISTRY[i].id === def.id) { idx = i; break; }
    }
    if (idx >= 0) _PANEL_REGISTRY[idx] = def;
    else _PANEL_REGISTRY.push(def);
    // Attach to any graphs that already exist on the page
    _LIVE_GRAPHS.forEach(function(g){
      try { if (g._attachSidebarPanel) g._attachSidebarPanel(def); } catch(e){
        if (typeof console !== 'undefined') console.warn('attach panel', def.id, e);
      }
    });
  }

  function listPanels(){ return _PANEL_REGISTRY.slice(); }

  // ─── Color palette (single source of truth) ─────────────────────────────
  var COL = {
    Dataset:     '#5a9e8f',
    Source:      '#c9955a',
    Category:    '#9e8fa0',
    FabricRecord:'#6b9bd2',
    Memory:      '#a78bfa',
    Session:     '#facc15',
    Activity:    '#94a3b8',
    Entity:      '#c97a5a',
    person:        '#8fb87a',
    organisation:  '#c9955a',
    technology:    '#5a9e8f',
    date:          '#38bdf8',
    year:          '#38bdf8',
    domain:        '#a78bfa',
    named_entity:  '#f472b6',
    'class':       '#facc15',
    'function':    '#facc15',
    module:        '#facc15',
    type_name:     '#94a3b8',
    constant:      '#94a3b8',
    // Memory record_type colors (lowercase as returned by API)
    message:       '#6ea8d8',
    event:         '#e09a55',
    observation:   '#5ec9a0',
    dag:           '#a78bfa',
    session:       '#e06060',
    fact:          '#5ec8f5',
    summary:       '#8df070',
    entity:        '#f0d060',
    _edge_HAS_ENTITY:    'rgba(201,122,90,0.75)',
    _edge_MENTIONED_IN:  'rgba(201,122,90,0.65)',
    _edge_CO_OCCURS:     'rgba(168,139,250,0.65)',
    _edge_LINKS_TO:      'rgba(56,189,248,0.7)',
    _edge_CONTAINS:      'rgba(90,158,143,0.75)',
    _edge_RELATED_TO:    'rgba(143,184,122,0.7)',
    _edge_SIMILAR_TO:    'rgba(143,184,122,0.7)',
    _edge_REFERENCES:    'rgba(56,189,248,0.7)',
    _edge_DEPENDS_ON:    'rgba(244,114,182,0.7)',
    _edge_DERIVED_FROM:  'rgba(167,139,250,0.7)',
    _edge_SHARES_TOPIC:  'rgba(250,204,21,0.6)',
    _edge_FOLLOWS_ACTIVITY: 'rgba(148,163,184,0.6)',
    _edge_default:       'rgba(190,190,190,0.55)',
    // Memory edge colors
    _edge_SESSION_CONTENT:    'rgba(94,201,160,0.75)',
    _edge_RESPONDS_TO:        'rgba(110,168,216,0.8)',
    _edge_FOLLOWS_ACTIVITY:   'rgba(148,163,184,0.7)',
    _edge_FOLLOWED_BY:        'rgba(224,154,85,0.75)',
    _edge_NEXT_IN_SESSION:    'rgba(224,154,85,0.7)',
    _edge_CAUSED_BY:          'rgba(224,96,96,0.75)',
    _edge_CAUSES:             'rgba(224,96,96,0.7)',
    _edge_TRIGGERED_BY:       'rgba(94,201,160,0.7)',
    _edge_THEN:               'rgba(167,139,250,0.75)',
    _edge_STARTS:             'rgba(167,139,250,0.65)',
  };

  function edgeColorByRel(rel) {
    // Convenience: return color for a given relationship type. Used by host UIs
    // for legends / list rows so colours match graph edges exactly.
    return edgeColor(rel);
  }

  function nodeColor(node){
    if (!node) return '#6a8fa0';
    if (node.type === 'Entity' && node.props && COL[node.props.type]) {
      return COL[node.props.type];
    }
    return COL[node.type] || COL[node.label] || '#6a8fa0';
  }

  function edgeColor(rel){
    if (!rel) return COL._edge_default;
    var key = '_edge_' + rel;
    if (COL[key]) return COL[key];
    if (/HAS_ENTITY/i.test(rel))     return COL._edge_HAS_ENTITY;
    if (/MENTIONED_IN/i.test(rel))   return COL._edge_MENTIONED_IN;
    if (/CO_OCCURS/i.test(rel))      return COL._edge_CO_OCCURS;
    if (/LINKS_TO/i.test(rel))       return COL._edge_LINKS_TO;
    if (/CONTAINS/i.test(rel))       return COL._edge_CONTAINS;
    if (/RELATED_TO/i.test(rel))     return COL._edge_RELATED_TO;
    if (/SIMILAR_TO/i.test(rel))     return COL._edge_SIMILAR_TO;
    if (/REFERENCES/i.test(rel))     return COL._edge_REFERENCES;
    if (/DEPENDS_ON/i.test(rel))     return COL._edge_DEPENDS_ON;
    if (/DERIVED_FROM/i.test(rel))   return COL._edge_DERIVED_FROM;
    if (/SHARES_TOPIC/i.test(rel))   return COL._edge_SHARES_TOPIC;
    if (/FOLLOWS_ACTIVITY/i.test(rel)) return COL._edge_FOLLOWS_ACTIVITY;
    return COL._edge_default;
  }

  function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}

  function throttle(fn, ms){
    var last = 0, pending = null;
    return function(){
      var args = arguments, self = this, now = Date.now();
      if (now - last >= ms) { last = now; fn.apply(self, args); }
      else if (!pending) {
        pending = setTimeout(function(){
          pending = null; last = Date.now();
          fn.apply(self, args);
        }, ms - (now - last));
      }
    };
  }

  // ════════════════════════════════════════════════════════════════════════
  // SHARED EVENT BUS (used by every graph instance unless host overrides)
  // ════════════════════════════════════════════════════════════════════════
  // Multiple delivery paths: parent postMessage forwarding (when iframed
  // inside the harness) AND a direct WebSocket fallback. The bus exposes
  // `subscribe(typePrefix, cb)` returning an unsubscribe function.

  var _sharedBus = null;
  function _getSharedBus(){
    if (_sharedBus) return _sharedBus;

    var listeners = []; // {prefix, cb}
    var dispatch = function(ev){
      if (!ev || !ev.type) return;
      for (var i = 0; i < listeners.length; i++) {
        var L = listeners[i];
        if (ev.type === L.prefix || ev.type.indexOf(L.prefix) === 0) {
          try { L.cb(ev); } catch(_){}
        }
      }
    };

    // Path 1: parent harness postMessage forwarding
    window.addEventListener('message', function(e){
      if (!e || !e.data) return;
      if (e.data.type === 'vera_fabric_event' && e.data.event) {
        dispatch(e.data.event);
      }
    });

    // Path 2: direct WS (only opened if a subscription is registered)
    var ws = null, wsTries = 0;
    function ensureWs(){
      if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
      try {
        var base = (window._veraBase || window.location.origin).replace(/^http/, 'ws');
        ws = new WebSocket(base + '/ws/mcp');
        ws.onopen = function(){
          try { ws.send(JSON.stringify({action:'subscribe_events'})); } catch(_){}
        };
        ws.onmessage = function(e){
          try {
            var raw = JSON.parse(e.data);
            var ev = raw.type === 'event' ? raw.data : raw;
            dispatch(ev);
          } catch(_){}
        };
        ws.onclose = function(){
          ws = null;
          if (wsTries < 5 && listeners.length) {
            wsTries++;
            setTimeout(ensureWs, 1500 * wsTries);
          }
        };
        ws.onerror = function(){};
      } catch(_){ ws = null; }
    }

    _sharedBus = {
      subscribe: function(prefix, cb){
        var entry = {prefix: prefix, cb: cb};
        listeners.push(entry);
        ensureWs();
        return function unsubscribe(){
          var i = listeners.indexOf(entry);
          if (i >= 0) listeners.splice(i, 1);
        };
      },
      // Allow hosts to manually push events into the bus (e.g. for testing)
      push: dispatch,
    };
    return _sharedBus;
  }

  // ════════════════════════════════════════════════════════════════════════
  // ACTION REGISTRY HELPERS
  // ════════════════════════════════════════════════════════════════════════
  // These are local-only fallbacks — used when the server registry is
  // unreachable. The server registry takes precedence when available.

  var _LOCAL_FALLBACK = {
    Dataset: [
      {id:'browse',           label:'Browse records',        icon:'▦',
       capability:'__local',  context:'Open this dataset in the Datasets tab.'},
      {id:'extract_entities', label:'Extract entities',      icon:'◉',
       capability:'fabric.entity_graph.extract_v2',
       args:{dataset_id:'$id'}, stream:'fabric.entity_graph.progress',
       options:[
         {name:'limit',     type:'int',  default:1000, label:'Max records'},
         {name:'overwrite', type:'bool', default:false, label:'Overwrite prior'},
       ],
       context:'Pulls named entities from records, links each to every record it appears in.'},
      {id:'run_loom',         label:'Run Loom',              icon:'⧖',
       capability:'fabric.loom.run',
       args:{dataset_a:'$id'}, stream:'fabric.loom.progress',
       options:[
         {name:'mode',        type:'select', default:'hybrid',
           options:['vector','keyword','hybrid']},
         {name:'min_score',   type:'float',  default:0.4},
         {name:'max_matches', type:'int',    default:100},
         {name:'persist',     type:'bool',   default:true},
       ],
       context:'Cross-dataset record stitching. Writes RELATED_TO edges.'},
      {id:'ai_analyse',       label:'AI Analyse Links',      icon:'✦',
       capability:'fabric.ai_analyse_links',
       args:{}, stream:'fabric.ai_analyse_links.progress',
       options:[
         {name:'max_pairs',   type:'int',   default:8},
         {name:'min_score',   type:'float', default:0.5},
         {name:'auto_stitch', type:'bool',  default:false},
       ],
       context:'LLM suggests related dataset pairs.'},
      {id:'unified_run',      label:'Unified run',           icon:'▶▶',
       capability:'fabric.graph.unified_run',
       args:{dataset_id:'$id'}, stream:'fabric.unified_run.progress',
       options:[
         {name:'include_loom',     type:'bool', default:true},
         {name:'include_ai_links', type:'bool', default:false},
         {name:'overwrite',        type:'bool', default:false},
       ],
       context:'Runs entity extraction → Loom → optional AI Analyse.'},
      {id:'run_cap',          label:'Run capability against dataset', icon:'▸',
       capability:'__dispatch',
       args:{dataset_id:'$id'},
       options:[
         {name:'capability', type:'string', default:'', label:'Capability name'},
         {name:'extra_args', type:'string', default:'{}', label:'Extra args (JSON)'},
       ],
       context:'Run any registered capability against this dataset.'},
      {id:'purge_entities',   label:'Purge entity state',    icon:'✕',
       capability:'fabric.entity_graph.purge', danger:true,
       args:{dataset_id:'$id'},
       options:[
         {name:'drop_entities',type:'bool',default:false,
           label:'Also drop now-orphan entity nodes'},
       ],
       confirm:'Delete all entity links for this dataset?',
       context:'Removes wrong/legacy entity rows for a clean re-extract.'},
    ],
    Source: [
      {id:'pull', label:'Pull source now', icon:'↓',
       capability:'fabric.source.pull',
       args:{source_id:'$id'}, stream:'fabric.source.progress'},
    ],
    Entity: [
      {id:'show_mentions', label:'Show mentions', icon:'⧉',
       capability:'fabric.entity_graph.mentions',
       args:{entity_id:'$id'},
       context:'List every record this entity actually appears in.'},
      {id:'merge', label:'Merge with another entity', icon:'⇆',
       capability:'fabric.entity_graph.merge',
       args:{entity_id:'$id'},
       options:[{name:'target_id',type:'string',label:'Target entity ID'}],
       confirm:'Merge this entity into the target?'},
    ],
    FabricRecord: [
      {id:'open_record', label:'Open record', icon:'▦', capability:'__local',
       context:'Navigate to this record in the dataset browser.'},
      {id:'extract_entities_record', label:'Re-extract entities from this record',
       icon:'◉', capability:'fabric.entity_graph.extract_record',
       args:{record_id:'$id'}, stream:'fabric.entity_graph.progress',
       context:'Run entity extraction on just this record.'},
      {id:'view_source', label:'Open source URL', icon:'↗', capability:'__local',
       context:'Open the original URL in a new tab (if available).'},
      {id:'find_related', label:'Find related records (Loom)', icon:'⧖',
       capability:'fabric.loom.record_match',
       args:{record_id:'$id'}, stream:'fabric.loom.progress',
       options:[
         {name:'mode', type:'select', default:'hybrid', options:['vector','keyword','hybrid']},
         {name:'max_matches', type:'int', default:10},
       ],
       context:'Find records across all datasets that are semantically related to this one.'},
      {id:'summarise', label:'Summarise with LLM', icon:'✦',
       capability:'fabric.record.summarise',
       args:{record_id:'$id'},
       context:'Generate an LLM summary of this record\'s content.'},
      {id:'run_cap', label:'Run capability against record', icon:'▸',
       capability:'__dispatch',
       args:{record_id:'$id'},
       options:[
         {name:'capability', type:'string', default:'', label:'Capability name'},
         {name:'extra_args', type:'string', default:'{}', label:'Extra args (JSON)'},
       ],
       context:'Run any registered capability with this record_id.'},
    ],
  };

  async function fetchActions(apiBase, label, nodeId){
    if (!label) return [];
    try {
      var qs = '?node_label=' + encodeURIComponent(label);
      if (nodeId) qs += '&node_id=' + encodeURIComponent(nodeId);
      var res = await fetch(apiBase + '/fabric/graph/node_actions' + qs);
      if (!res.ok) throw new Error('http ' + res.status);
      var data = await res.json();
      if (data && Array.isArray(data.actions) && data.actions.length) {
        return data.actions;
      }
    } catch(_){}
    return _LOCAL_FALLBACK[label] || [];
  }

  // ════════════════════════════════════════════════════════════════════════
  // GRAPH INSTANCE
  // ════════════════════════════════════════════════════════════════════════
  // ─── Inject component CSS ──────────────────────────────────────────────
  var _cssInjected = false;
  function _injectCSS(){
    if(_cssInjected)return; _cssInjected=true;
    var s=document.createElement('style');
    s.textContent = [
      '.vg-left{width:220px;max-width:100%;flex-shrink:0;background:var(--bg1,#1f1d1a);border-right:1px solid var(--border,#3a3530);display:flex;flex-direction:column;overflow:hidden;transition:width .2s ease,min-width .2s ease}',
      '.vg-sb-panel .vg-left{width:100%!important;max-width:100%!important;border-right:none!important;flex:1}',
      '.vg-left.collapsed{width:32px;min-width:32px}',
      '.vg-left.collapsed .vg-left-body{display:none}',
      '.vg-left.collapsed .vg-left-title{display:none}',
      '.vg-left.collapsed .vg-stat-bar{display:none}',
      '.vg-left.collapsed .vg-left-toggle{transform:rotate(180deg)}',
      '.vg-left-hd{padding:6px 10px;border-bottom:1px solid var(--border,#3a3530);font-size:9px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.8px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}',
      '.vg-left-body{flex:1;overflow-y:auto;padding:6px 8px}',
      '.vg-left-body::-webkit-scrollbar{width:3px} .vg-left-body::-webkit-scrollbar-thumb{background:var(--border2,#4a4540)}',
      '.vg-sl{font-size:8.5px;letter-spacing:.08em;color:var(--dim,#6a6058);text-transform:uppercase;margin:8px 0 4px;display:flex;align-items:center;gap:5px}',
      '.vg-chips{display:flex;flex-wrap:wrap;gap:2px;margin-bottom:4px}',
      '.vg-chip{font-family:var(--mono,monospace);font-size:8px;padding:1px 6px;border-radius:8px;border:1px solid var(--border,#3a3530);background:transparent;color:var(--dim,#6a6058);cursor:pointer;user-select:none;transition:all .12s;white-space:nowrap}',
      '.vg-chip.on{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f);background:rgba(90,158,143,.1)}',
      '.vg-chip .cc{opacity:.5;margin-left:3px;font-size:7.5px}',
      '.vg-chip i{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:4px;vertical-align:middle}',
      '.vg-rel-wrap{margin-bottom:6px}',
      '.vg-rel-wrap input[type=range]{width:100%;height:14px;cursor:pointer}',
      '.vg-rel-val{font-family:var(--mono,monospace);font-size:8.5px;color:var(--dim,#6a6058)}',
      '.vg-ctrl{display:flex;align-items:center;gap:5px;margin-bottom:4px}',
      '.vg-ctrl label{font-size:9px;color:var(--dim2,#8a7e70);min-width:46px;flex-shrink:0;font-family:var(--mono,monospace)}',
      '.vg-ctrl input[type=range]{flex:1;accent-color:var(--acc,#5a9e8f);height:14px;cursor:pointer}',
      '.vg-ctrl select{flex:1;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);font-family:var(--mono,monospace);font-size:9px;padding:2px 4px;border-radius:3px}',
      '.vg-view-chip{font-family:var(--mono,monospace);font-size:8.5px;padding:2px 7px;border-radius:2px;border:1px solid var(--border,#3a3530);background:transparent;color:var(--dim,#6a6058);cursor:pointer;transition:all .12s}',
      '.vg-view-chip.on{border-color:var(--acc2,#8fb87a);color:var(--acc2,#8fb87a);background:rgba(143,184,122,.1)}',
      '.vg-sp{font-family:var(--mono,monospace);font-size:8.5px;padding:3px 6px;border:1px solid var(--border,#3a3530);border-radius:3px;margin-bottom:2px;background:var(--bg2,#272421);cursor:pointer;transition:all .12s}',
      '.vg-sp:hover{border-color:var(--acc,#5a9e8f)} .vg-sp.on{border-color:var(--acc2,#8fb87a);background:rgba(143,184,122,.1)}',
      '.vg-canvas-area{flex:1;min-width:0;display:flex;flex-direction:column;position:relative;background:var(--bg0,#181614)}',
      '.vg-gp{display:flex;gap:2px;margin-bottom:6px}',
      '.vg-gp .tb{font-family:var(--mono,monospace);font-size:8.5px;padding:2px 7px;flex:1;text-align:center;border-radius:3px;background:var(--bg2,#272421);border:1px solid var(--border,#3a3530);color:var(--dim2,#8a7e70);cursor:pointer;transition:all .12s}',
      '.vg-gp .tb:hover{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f)}',
      '.vg-gp .tb.on{border-color:var(--acc2,#8fb87a);color:var(--acc2,#8fb87a);background:rgba(143,184,122,.08)}',
      '.vg-ft{display:flex;align-items:flex-start;gap:4px;padding:3px 6px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);border-radius:3px;margin-bottom:2px;cursor:grab;transition:border-color .12s;user-select:none}',
      '.vg-ft:hover{border-color:var(--acc,#5a9e8f)}',
      '.vg-ft-k{font-family:var(--mono,monospace);font-size:7.5px;color:var(--dim,#6a6058);min-width:48px;flex-shrink:0;text-transform:uppercase;padding-top:1px}',
      '.vg-ft-v{font-family:var(--mono,monospace);font-size:9px;color:var(--text,#ddd5c8);word-break:break-all;line-height:1.35;max-height:40px;overflow:hidden}',
      '.vg-cap-sel{width:100%;font-size:9px;padding:2px 5px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:3px;margin-bottom:4px;font-family:var(--mono,monospace)}',
      '.vg-cap-run{width:100%;font-size:9px;padding:4px;background:rgba(90,158,143,.1);border:1px solid var(--acc,#5a9e8f);color:var(--acc,#5a9e8f);border-radius:3px;cursor:pointer;font-family:var(--mono,monospace)}',
      '.vg-cap-run:hover{background:rgba(90,158,143,.18)}',
      '.vg-search{width:100%;font-size:9.5px;padding:3px 6px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:3px;margin-bottom:6px;font-family:var(--mono,monospace);outline:none}',
      '.vg-search:focus{border-color:var(--acc,#5a9e8f)}',
      // ── Modular sidebar (companion-plugin panels) ──────────────────────────
      '.vg-sidebar{display:flex;flex-direction:row;flex-shrink:0;height:100%;background:var(--bg1,#1f1d1a);border-right:1px solid var(--border,#3a3530)}',
      '.vg-sb-rail{width:34px;flex-shrink:0;background:var(--bg2,#272421);border-right:1px solid var(--border,#3a3530);display:flex;flex-direction:column;align-items:center;padding-top:6px;gap:2px;overflow-y:auto;overflow-x:hidden}',
      '.vg-sb-tab{width:28px;height:28px;display:flex;align-items:center;justify-content:center;border-radius:4px;cursor:pointer;color:var(--dim,#6a6058);font-size:14px;border:1px solid transparent;transition:all .12s;position:relative;user-select:none}',
      '.vg-sb-tab:hover{color:var(--acc,#5a9e8f);border-color:var(--border,#3a3530)}',
      '.vg-sb-tab.on{color:var(--acc2,#8fb87a);background:rgba(143,184,122,.12);border-color:var(--acc2,#8fb87a)}',
      '.vg-sb-tab .vg-sb-tip{position:absolute;left:34px;top:50%;transform:translateY(-50%);background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);font-size:9px;padding:2px 6px;border-radius:3px;white-space:nowrap;pointer-events:none;opacity:0;transition:opacity .12s;z-index:30;font-family:var(--mono,monospace)}',
      '.vg-sb-tab:hover .vg-sb-tip{opacity:1}',
      '.vg-sb-panels{width:300px;flex-shrink:0;overflow:hidden;display:flex;flex-direction:column;transition:width .2s ease}',
      '.vg-sidebar.collapsed .vg-sb-panels{width:0}',
      '.vg-sb-panel{flex:1;overflow-y:auto;display:none;flex-direction:column}',
      '.vg-sb-panel.on{display:flex}',
      '.vg-sb-panel::-webkit-scrollbar{width:4px} .vg-sb-panel::-webkit-scrollbar-thumb{background:var(--border2,#4a4540)}',
      '.vg-sb-panel-hd{padding:7px 10px;border-bottom:1px solid var(--border,#3a3530);font-size:9px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.8px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}',
      '.vg-sb-panel-body{flex:1;overflow-y:auto;padding:8px 10px}',
      // ── Bottom drawers ─────────────────────────────────────────────────────
      '.vg-bottom-area{flex-shrink:0;display:flex;flex-direction:column;background:var(--bg0,#181614);border-top:1px solid var(--border,#3a3530)}',
      '.vg-bd-rail{display:flex;align-items:center;gap:1px;padding:0 6px;height:26px;background:var(--bg1,#1f1d1a);border-bottom:1px solid var(--border,#3a3530);flex-shrink:0}',
      '.vg-bd-tab{display:inline-flex;align-items:center;gap:4px;padding:2px 9px;font-size:9px;font-family:var(--mono,monospace);color:var(--dim,#6a6058);cursor:pointer;border-radius:3px 3px 0 0;border:1px solid transparent;border-bottom:none;user-select:none;transition:all .12s}',
      '.vg-bd-tab:hover{color:var(--acc,#5a9e8f)}',
      '.vg-bd-tab.on{color:var(--acc2,#8fb87a);border-color:var(--border,#3a3530);background:var(--bg0,#181614)}',
      '.vg-bd-tab .vg-bd-badge{margin-left:2px;font-size:7.5px;background:var(--acc,#5a9e8f);color:#10100e;border-radius:6px;padding:0 4px;opacity:0;transition:opacity .12s}',
      '.vg-bd-tab.has-content .vg-bd-badge{opacity:1}',
      '.vg-bd-rail-right{margin-left:auto;display:flex;align-items:center;gap:4px}',
      '.vg-bd-resize{cursor:ns-resize;font-size:9px;color:var(--dim,#6a6058);padding:0 6px;user-select:none}',
      '.vg-bd-resize:hover{color:var(--acc,#5a9e8f)}',
      '.vg-bd-collapse{font-size:10px;color:var(--dim,#6a6058);cursor:pointer;padding:0 4px;user-select:none;line-height:1}',
      '.vg-bd-collapse:hover{color:var(--acc,#5a9e8f)}',
      '.vg-bd-panels{overflow:hidden;transition:height .18s ease;flex-shrink:0}',
      '.vg-bd-panels.collapsed{height:0!important}',
      '.vg-bd-panel{display:none;flex-direction:column;height:100%;max-height:100%;min-height:0;overflow:hidden}',
      '.vg-bd-panel.on{display:flex}',
      // Terminal panel
      '.vg-bd-term{flex:1;overflow-y:auto;padding:5px 8px;font-family:var(--mono,ui-monospace,monospace);font-size:9px;line-height:1.55;background:var(--bg0,#181614)}',
      '.vg-bd-term::-webkit-scrollbar{width:3px} .vg-bd-term::-webkit-scrollbar-thumb{background:var(--border2,#4a4540)}',
      '.vg-bd-term-bar{display:flex;align-items:center;gap:6px;padding:2px 8px;border-bottom:1px solid var(--border,#3a3530);background:var(--bg1,#1f1d1a);font-size:8.5px;flex-shrink:0}',
      '.vg-bd-term-bar button{font-size:8px;padding:1px 6px;background:var(--bg2,#272421);border:1px solid var(--border,#3a3530);color:var(--dim,#6a6058);border-radius:2px;cursor:pointer}',
      '.vg-bd-term-bar button:hover{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f)}',
      // Table panel
      '.vg-bd-tbl-wrap{flex:1;overflow:auto}',
      '.vg-bd-tbl{border-collapse:collapse;width:100%;font-size:9.5px;font-family:var(--mono,monospace)}',
      '.vg-bd-tbl th{position:sticky;top:0;background:var(--bg2,#272421);color:var(--acc2,#8fb87a);text-align:left;padding:4px 8px;border-bottom:1px solid var(--border,#3a3530);white-space:nowrap;font-weight:600;letter-spacing:.3px}',
      '.vg-bd-tbl td{padding:3px 8px;border-bottom:1px solid rgba(58,53,48,.5);color:var(--text,#ddd5c8);vertical-align:top;max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}',
      '.vg-bd-tbl td.wrap{white-space:pre-wrap;max-width:none}',
      '.vg-bd-tbl tr:hover td{background:var(--bg2,#272421)}',
      '.vg-bd-tbl-bar{display:flex;align-items:center;gap:8px;padding:4px 8px;border-bottom:1px solid var(--border,#3a3530);background:var(--bg1,#1f1d1a);font-size:9px;color:var(--dim,#6a6058);flex-shrink:0}',
      '.vg-bd-tbl-bar input{flex:1;min-width:0;font-size:9px;padding:2px 5px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:2px}',
      // Content panel
      '.vg-bd-content{flex:1;overflow-y:auto;padding:10px 14px;font-size:11px;line-height:1.65;color:var(--text,#ddd5c8);white-space:pre-wrap;word-break:break-word;font-family:var(--sans,system-ui,sans-serif);background:var(--bg0,#181614)}',
      '.vg-bd-content::-webkit-scrollbar{width:4px} .vg-bd-content::-webkit-scrollbar-thumb{background:var(--border2,#4a4540)}',
      '.vg-bd-content-bar{display:flex;align-items:center;gap:8px;padding:4px 8px;border-bottom:1px solid var(--border,#3a3530);background:var(--bg1,#1f1d1a);font-size:9px;flex-shrink:0}',
      '.vg-bd-content-title{font-size:10px;font-weight:600;color:var(--acc,#5a9e8f);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    ].join('\n');
    document.head.appendChild(s);
  }

  function createGraph(container, opts){
    opts = opts || {};
    var apiBase = opts.apiBase || (window._veraBase || '');

    // ── State ────────────────────────────────────────────────────────────
    var state = {
      nodes: [], edges: [],
      drag: null, pan: null,
      off: { x: 0, y: 0 }, scale: 1,
      hov: null, selected: null,
      searchHighlight: new Set(),
      expanded: {},
      tickCount: 0, frozen: false, stopped: false, animHandle: null,
      // Auto-anneal: how many more times the simulation may re-energise itself
      // before it's allowed to freeze. Each cycle is equivalent to one press of
      // the Re-layout button — it lets the layout escape a mediocre local
      // arrangement and settle into a better one. Set on load(); see tick().
      settleCycles: 0,
      nodeIndex: {},
      currentLayer: opts.defaultLayer || 'fabric',
      currentParams: opts.layerOpts || {},
      // Tracks whether the visible graph was loaded via fetchSnapshot (so the
      // post-action refetch knows it can safely reload the same scoped view).
      // If the graph was populated some other way (direct load(), a dataset
      // scope set by the host panel, a loom view, etc.) we must NOT refetch a
      // generic full-graph snapshot — that replaces the user's view with the
      // oldest 200 nodes in Neo4j.
      loadedViaSnapshot: false,
      // Action machinery
      actionsEnabled: opts.actionsEnabled !== false,
      activeActions: [],     // currently-running action contexts
      actionsCache:  {},     // label -> [actions]  (populated lazily)
    };

    // ── Build DOM ────────────────────────────────────────────────────────
    container.classList.add('vera-graph-host');
    if (!container.style.position) container.style.position = 'relative';

    var DEFAULT_HEIGHT = opts.height || 420;
    var FILL_MODE = DEFAULT_HEIGHT === 'fill' || DEFAULT_HEIGHT === '100%';
    var canvasHeightCss = FILL_MODE ? '100%' : (DEFAULT_HEIGHT + 'px');
    var wrapHeightCss = FILL_MODE ? 'flex:1;min-height:0' : '';
    var headerHTML = '';
    if (opts.showSearch !== false || opts.showLayerToggle || opts.showLegend !== false) {
      headerHTML =
        '<div class="vg-header" style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-bottom:5px">' +
        (opts.showLayerToggle && opts.layers && opts.layers.length > 1 ?
          '<select class="vg-layer" style="font-size:10px;padding:2px 6px;background:var(--bg2);color:var(--text);border:1px solid var(--border);border-radius:3px">' +
            opts.layers.map(function(l){
              var label = ({fabric:'Fabric',entity:'Entities',aux:'Aux',memory:'Memory',net:'Network'})[l] || l;
              return '<option value="' + esc(l) + '"' + (l === state.currentLayer ? ' selected' : '') + '>' + esc(label) + '</option>';
            }).join('') +
          '</select>' : '') +
        (opts.showSearch !== false ?
          '<div class="vg-search-wrap" style="position:relative;flex:1;min-width:160px;display:flex;align-items:center;gap:4px">' +
            '<input class="vg-search vg-top-search" placeholder="Search nodes..." style="flex:1;min-width:90px;font-size:10px;padding:3px 6px;background:var(--bg2);color:var(--text);border:1px solid var(--border);border-radius:3px">' +
            '<button class="vg-btn vg-search-mode" data-mode="list" title="Search mode: List + zoom to result. Click to switch to Highlight." style="font-size:9px;padding:2px 6px;background:var(--bg2);color:var(--acc,#5a9e8f);border:1px solid var(--border);border-radius:3px;cursor:pointer;white-space:nowrap">List</button>' +
            '<select class="vg-search-depth" title="Highlight depth (hops from match)" style="display:none;font-size:9px;padding:2px 3px;background:var(--bg2);color:var(--text);border:1px solid var(--border);border-radius:3px">' +
              '<option value="0">0</option><option value="1" selected>1</option><option value="2">2</option><option value="3">3</option></select>' +
            '<div class="vg-search-results" style="display:none;position:absolute;top:100%;left:0;right:0;margin-top:2px;max-height:240px;overflow-y:auto;background:var(--bg1,#1f1d1a);border:1px solid var(--border,#3a3530);border-radius:4px;z-index:50;box-shadow:0 4px 14px rgba(0,0,0,.5)"></div>' +
          '</div>' : '') +
        '<button class="vg-btn vg-relayout" title="Re-energise layout" style="font-size:9px;padding:2px 8px;background:var(--bg2);color:var(--text);border:1px solid var(--border);border-radius:3px;cursor:pointer">Re-layout</button>' +
        '<button class="vg-btn vg-fit"      title="Fit to view"        style="font-size:9px;padding:2px 8px;background:var(--bg2);color:var(--text);border:1px solid var(--border);border-radius:3px;cursor:pointer">Fit</button>' +
        '<span class="vg-meta" style="font-size:9px;color:var(--dim2);font-family:var(--mono,monospace)"></span>' +
        '</div>';
    }

    // ── Inject component CSS ────────────────────────────────────────────
    _injectCSS();

    // ── Build 3-pane layout: left panel + canvas + right detail ─────────
    var showLeft = opts.showLeftPanel !== false;
    var leftHTML = '';
    if (showLeft && opts.filtersOnly) {
      // Compact panel: just the filter section. Use flex:1 so it fills available width.
      leftHTML = '<div class="vg-left vg-left-filters" style="width:auto;flex:1">' +
        '<div class="vg-left-hd"><span class="vg-left-title">Filters</span><span class="vg-stat-bar" style="font-size:8.5px;color:var(--dim,#6a6058);font-family:var(--mono,monospace);flex:1;text-align:right"></span>' +
        '<button class="vg-left-toggle" style="background:none;border:none;color:var(--dim,#6a6058);cursor:pointer;font-size:13px;padding:0 2px;line-height:1" title="Collapse panel">\u25c0</button></div>' +
        '<div class="vg-left-body">' +
          '<div class="vg-sl" style="display:flex;align-items:center;justify-content:space-between;gap:6px">Filter <button class="vg-filter-mode tb" data-mode="exclude" title="Switch between excluding (hide the kinds you click) and including (show only the kinds you click)" style="font-size:8px;padding:1px 6px;cursor:pointer">Exclude</button></div>' +
          '<div class="vg-sl">Node Types <span class="vg-type-cnt" style="font-size:7.5px"></span></div>' +
          '<div class="vg-chips vg-type-chips"></div>' +
          '<div class="vg-sl vg-ent-sl" style="display:none">Entities <span class="vg-ent-cnt" style="font-size:7.5px"></span></div>' +
          '<div class="vg-chips vg-ent-chips" style="display:none"></div>' +
          '<div class="vg-sl">Edge Types <span class="vg-edge-cnt" style="font-size:7.5px"></span></div>' +
          '<div class="vg-chips vg-edge-chips"></div>' +
          '<div class="vg-sl vg-layers-sl" style="display:none">Layers <span style="font-size:7.5px;color:var(--dim,#6a6058)">\u25c9 show \u26a1 physics</span></div>' +
          '<div class="vg-layers-list" style="display:none;margin-bottom:4px"></div>' +
          '<div class="vg-sl vg-rel-sl" style="display:none">Relevance fade <span class="vg-rel-val"></span></div>' +
          '<div class="vg-rel-wrap" style="display:none"><input type="range" class="vg-rel-slider" min="0" max="100" value="0"></div>' +
          // View mode + per-layout controls (shared with the full panel)
          '<div class="vg-sl">View Mode</div>' +
          '<div style="display:flex;gap:2px;flex-wrap:wrap;margin-bottom:6px">' +
            '<span class="vg-view-chip on" data-layout="default">Default</span>' +
            '<span class="vg-view-chip" data-layout="force-axis">Force+Axis</span>' +
            '<span class="vg-view-chip" data-layout="timeline">Timeline</span>' +
            '<span class="vg-view-chip" data-layout="hierarchy">Hierarchy</span>' +
            '<span class="vg-view-chip" data-layout="radial">Radial</span>' +
            '<span class="vg-view-chip" data-layout="latent-map" title="Static latent map (set by WorldView)">Latent</span>' +
          '</div>' +
          // Per-layout controls
          '<div class="vg-layout-ctrls">' +
            '<div class="vg-lc-force-axis" style="display:none">' +
              '<div class="vg-ctrl"><label>X axis</label><select class="vg-ax-x"><option value="type">Type</option><option value="cluster">Cluster</option><option value="layer">Layer</option><option value="degree">Degree</option><option value="label">Label A-Z</option><option value="time">Time</option><option value="importance">Importance</option><option value="source">Source</option><option value="category">Category</option></select></div>' +
              '<div class="vg-ctrl"><label>Y axis</label><select class="vg-ax-y"><option value="cluster">Cluster</option><option value="type">Type</option><option value="layer">Layer</option><option value="degree">Degree</option><option value="importance">Importance</option><option value="session">Session</option><option value="label">Label A-Z</option></select></div>' +
              '<div class="vg-ctrl"><label>Spread</label><input type="range" class="vg-spread" min="60" max="600" value="200"></div>' +
              '<div class="vg-ctrl"><label>Gravity</label><input type="range" class="vg-gravity" min="0" max="30" value="3" step="1"></div>' +
            '</div>' +
            '<div class="vg-lc-timeline" style="display:none">' +
              '<div class="vg-ctrl"><label>Lane ht</label><input type="range" class="vg-tl-lane" min="40" max="200" value="80"></div>' +
              '<div class="vg-ctrl"><label>px/hr</label><input type="range" class="vg-tl-scale" min="10" max="300" value="60"></div>' +
            '</div>' +
            '<div class="vg-lc-hierarchy" style="display:none">' +
              '<div class="vg-ctrl"><label>Level gap</label><input type="range" class="vg-hr-gap" min="60" max="300" value="130"></div>' +
              '<div class="vg-ctrl"><label>Node gap</label><input type="range" class="vg-hr-node" min="30" max="150" value="60"></div>' +
              '<div class="vg-ctrl"><label>Root</label><select class="vg-hr-root"><option value="session">Session</option><option value="Dataset">Dataset</option><option value="message">Message</option><option value="dag">DAG</option></select></div>' +
            '</div>' +
            '<div class="vg-lc-radial" style="display:none">' +
              '<div class="vg-ctrl"><label>Radius</label><input type="range" class="vg-rd-radius" min="80" max="500" value="200"></div>' +
            '</div>' +
          '</div>' +
          // Search
          '<div class="vg-sl" style="display:none">Search</div>' +
          '<input type="search" class="vg-search vg-node-search" placeholder="Filter nodes..." style="display:none">' +
        '</div>' +
      '</div>';
    } else if (showLeft) {
      leftHTML = '<div class="vg-left">' +
        '<div class="vg-left-hd"><span class="vg-left-title">Graph</span><span class="vg-stat-bar" style="font-size:8.5px;color:var(--dim,#6a6058);font-family:var(--mono,monospace);flex:1;text-align:right"></span>' +
        '<button class="vg-left-toggle" style="background:none;border:none;color:var(--dim,#6a6058);cursor:pointer;font-size:13px;padding:0 2px;line-height:1" title="Collapse panel">\u25c0</button></div>' +
        '<div class="vg-left-body">' +
          // Graph source picker
          '<div class="vg-sl">Source</div>' +
          '<div class="vg-gp">' +
            '<span class="tb on" data-src="fabric">Fabric</span>' +
            '<span class="tb" data-src="memory">Memory</span>' +
            '<span class="tb" data-src="net">Net</span>' +
          '</div>' +
          // Memory mode (hidden until memory selected)
          '<div class="vg-mem-mode" style="display:none;margin-bottom:6px">' +
            '<select class="vg-mem-sel" style="width:100%;font-size:9px;padding:2px 4px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:3px;font-family:var(--mono,monospace)">' +
              '<option value="session">Current session</option>' +
              '<option value="recent">Recent (24h)</option>' +
              '<option value="all">All</option>' +
            '</select>' +
          '</div>' +
          // Filter chips
          '<div class="vg-sl" style="display:flex;align-items:center;justify-content:space-between;gap:6px">Filter <button class="vg-filter-mode tb" data-mode="exclude" title="Switch between excluding (hide the kinds you click) and including (show only the kinds you click)" style="font-size:8px;padding:1px 6px;cursor:pointer">Exclude</button></div>' +
          '<div class="vg-sl">Node Types <span class="vg-type-cnt" style="font-size:7.5px"></span></div>' +
          '<div class="vg-chips vg-type-chips"></div>' +
          '<div class="vg-sl vg-ent-sl" style="display:none">Entities <span class="vg-ent-cnt" style="font-size:7.5px"></span></div>' +
          '<div class="vg-chips vg-ent-chips" style="display:none"></div>' +
          '<div class="vg-sl">Edge Types <span class="vg-edge-cnt" style="font-size:7.5px"></span></div>' +
          '<div class="vg-chips vg-edge-chips"></div>' +
          '<div class="vg-sl vg-layers-sl" style="display:none">Layers <span style="font-size:7.5px;color:var(--dim,#6a6058)">\u25c9 show \u26a1 physics</span></div>' +
          '<div class="vg-layers-list" style="display:none;margin-bottom:4px"></div>' +
          '<div class="vg-sl vg-rel-sl" style="display:none">Relevance fade <span class="vg-rel-val"></span></div>' +
          '<div class="vg-rel-wrap" style="display:none"><input type="range" class="vg-rel-slider" min="0" max="100" value="0"></div>' +
          // View mode
          '<div class="vg-sl">View Mode</div>' +
          '<div style="display:flex;gap:2px;flex-wrap:wrap;margin-bottom:6px">' +
            '<span class="vg-view-chip on" data-layout="default">Default</span>' +
            '<span class="vg-view-chip" data-layout="force-axis">Force+Axis</span>' +
            '<span class="vg-view-chip" data-layout="timeline">Timeline</span>' +
            '<span class="vg-view-chip" data-layout="hierarchy">Hierarchy</span>' +
            '<span class="vg-view-chip" data-layout="radial">Radial</span>' +
            '<span class="vg-view-chip" data-layout="latent-map" title="Static latent map (set by WorldView)">Latent</span>' +
          '</div>' +
          // Per-layout controls
          '<div class="vg-layout-ctrls">' +
            '<div class="vg-lc-force-axis" style="display:none">' +
              '<div class="vg-ctrl"><label>X axis</label><select class="vg-ax-x"><option value="type">Type</option><option value="cluster">Cluster</option><option value="layer">Layer</option><option value="degree">Degree</option><option value="label">Label A-Z</option><option value="time">Time</option><option value="importance">Importance</option><option value="source">Source</option><option value="category">Category</option></select></div>' +
              '<div class="vg-ctrl"><label>Y axis</label><select class="vg-ax-y"><option value="cluster">Cluster</option><option value="type">Type</option><option value="layer">Layer</option><option value="degree">Degree</option><option value="importance">Importance</option><option value="session">Session</option><option value="label">Label A-Z</option></select></div>' +
              '<div class="vg-ctrl"><label>Spread</label><input type="range" class="vg-spread" min="60" max="600" value="200"></div>' +
              '<div class="vg-ctrl"><label>Gravity</label><input type="range" class="vg-gravity" min="0" max="30" value="3" step="1"></div>' +
            '</div>' +
            '<div class="vg-lc-timeline" style="display:none">' +
              '<div class="vg-ctrl"><label>Lane ht</label><input type="range" class="vg-tl-lane" min="40" max="200" value="80"></div>' +
              '<div class="vg-ctrl"><label>px/hr</label><input type="range" class="vg-tl-scale" min="10" max="300" value="60"></div>' +
            '</div>' +
            '<div class="vg-lc-hierarchy" style="display:none">' +
              '<div class="vg-ctrl"><label>Level gap</label><input type="range" class="vg-hr-gap" min="60" max="300" value="130"></div>' +
              '<div class="vg-ctrl"><label>Node gap</label><input type="range" class="vg-hr-node" min="30" max="150" value="60"></div>' +
              '<div class="vg-ctrl"><label>Root</label><select class="vg-hr-root"><option value="session">Session</option><option value="Dataset">Dataset</option><option value="message">Message</option><option value="dag">DAG</option></select></div>' +
            '</div>' +
            '<div class="vg-lc-radial" style="display:none">' +
              '<div class="vg-ctrl"><label>Radius</label><input type="range" class="vg-rd-radius" min="80" max="500" value="200"></div>' +
            '</div>' +
          '</div>' +
          // Sessions (memory mode)
          '<div class="vg-sessions-sec" style="display:none">' +
            '<div class="vg-sl">Sessions <button class="tb" style="font-size:8px;padding:1px 5px;margin-left:auto" data-load-sess>Load</button></div>' +
            '<input type="search" class="vg-search vg-sp-search" placeholder="Filter sessions..." style="display:none">' +
            '<div class="vg-sp-list" style="max-height:160px;overflow-y:auto"></div>' +
          '</div>' +
          // Tags
          '<div class="vg-tags-sec">' +
            '<div class="vg-sl">Tags</div>' +
            '<div class="vg-chips vg-tag-chips" style="max-height:80px;overflow-y:auto"></div>' +
          '</div>' +
          // Left-panel sections (populated after _sectionHTML is defined)
          '<div class="vg-left-sections"></div>' +
          // Search
          '<div class="vg-sl" style="display:none">Search</div>' +
          '<input type="search" class="vg-search vg-node-search" placeholder="Filter nodes..." style="display:none">' +
          // Debug
          '<div class="vg-sl">Info</div>' +
          '<div class="vg-debug" style="font-family:var(--mono,monospace);font-size:8.5px;color:var(--dim,#6a6058);line-height:1.7"></div>' +
        '</div>' +
      '</div>';
    }

    if (FILL_MODE) {
      container.style.display = 'flex';
      container.style.flexDirection = 'row';
      container.style.height = '100%';
    } else {
      container.style.display = 'flex';
      container.style.flexDirection = 'row';
    }
    container.innerHTML = leftHTML +
      '<div class="vg-canvas-area" style="display:flex;flex-direction:column;' + (FILL_MODE ? 'flex:1;min-width:0;min-height:0;overflow:hidden' : '') + '">' + headerHTML +
      '<div class="vg-canvas-wrap" style="position:relative;flex:1;min-height:0;' + (FILL_MODE ? '' : 'height:' + canvasHeightCss) + '">' +
        '<canvas class="vg-canvas" style="width:100%;height:100%;background:var(--bg0);border:1px solid var(--border);border-radius:var(--radius,4px);cursor:grab;display:block"></canvas>' +
        '<div class="vg-tooltip" style="position:absolute;display:none;background:var(--bg2);border:1px solid var(--border);border-radius:3px;padding:4px 8px;font-size:9.5px;color:var(--text);pointer-events:none;z-index:5;max-width:220px;line-height:1.4"></div>' +
        '<div class="vg-detail" style="position:absolute;top:0;right:0;width:320px;height:100%;background:var(--bg1);border-left:1px solid var(--border);overflow-y:auto;display:none;font-size:10.5px"></div>' +
        '<div class="vg-actionbar" style="position:absolute;left:0;bottom:0;right:0;display:none;background:var(--bg1);border-top:1px solid var(--border);max-height:42%;overflow-y:auto;font-size:10px"></div>' +
      '</div>' +
      (opts.showLegend !== false ?
        '<div class="vg-legend"></div>' : '') +
      '<div class="vg-bottom-area" style="display:none">' +
        '<div class="vg-bd-rail"></div>' +
        '<div class="vg-bd-panels"></div>' +
      '</div>' +
      '</div>'; // close vg-canvas-area

    var canvas    = container.querySelector('.vg-canvas');
    var tooltip   = container.querySelector('.vg-tooltip');
    var detailEl  = container.querySelector('.vg-detail');
    var actionEl  = container.querySelector('.vg-actionbar');
    var metaEl    = container.querySelector('.vg-meta');
    var legendEl  = container.querySelector('.vg-legend');
    var searchEl  = container.querySelector('.vg-top-search') || container.querySelector('.vg-search');
    var layerEl   = container.querySelector('.vg-layer');
    var relayoutBtn = container.querySelector('.vg-relayout');
    var fitBtn    = container.querySelector('.vg-fit');
    var gravityEl = container.querySelector('.vg-gravity');

    var ctx = canvas.getContext('2d');
    var W, H, DPR;

    function resize(){
      var rect = canvas.getBoundingClientRect();
      var rw = rect.width || canvas.offsetWidth;
      var rh = rect.height || canvas.offsetHeight;
      // Guard: if the canvas reports 0 size the parent is hidden (display:none).
      // Setting canvas.width/height to 0 destroys the context backing store and
      // resets the transform — skip the resize to preserve the last good state.
      if (!rw || !rh) return;
      W = rw;
      H = rh;
      DPR = window.devicePixelRatio || 1;
      canvas.width  = W * DPR;
      canvas.height = H * DPR;
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    }
    resize();
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(function(){ resize(); if (!state.stopped) { state.frozen = false; state.tickCount = 0; } else { startTick(); } }).observe(canvas);
    }

    // ── Render ───────────────────────────────────────────────────────────
    function draw(){
      ctx.clearRect(0, 0, W, H);
      ctx.save();
      ctx.translate(state.off.x, state.off.y);
      ctx.scale(state.scale, state.scale);

      var nm = {};
      state.nodes.forEach(function(n){ nm[n.id] = n; });
      state.edges.forEach(function(e){
        var a = nm[e.from], b = nm[e.to];
        if (!a || !b) return;
        if (a._hidden || b._hidden) return;
        if (e._layer && !_layerVisible(e._layer)) return;
        if (state._edgeHidden && state._edgeHidden(e.rel)) return;
        var isSel = state.selected && (state.selected.id === a.id || state.selected.id === b.id);
        // Apply edge style function if provided
        var es = opts.edgeStyleFn ? opts.edgeStyleFn(e) : null;
        var ec = (es && es.color) || edgeColor(e.rel);
        var lw = isSel ? 1.8 : ((es && es.width) || 1);
        ctx.beginPath();
        if (e._dashed) ctx.setLineDash([4, 3]);
        else if (es && es.dash) ctx.setLineDash(es.dash);
        else ctx.setLineDash([]);
        ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
        ctx.strokeStyle = ec;
        ctx.lineWidth   = lw;
        ctx.stroke();
        ctx.setLineDash([]);
        if (e.rel && state.scale > 0.6) {
          ctx.font = '7px monospace';
          ctx.fillStyle = 'rgba(160,150,130,.7)';
          ctx.textAlign = 'center';
          ctx.fillText(String(e.rel).slice(0, 14), (a.x + b.x) / 2, (a.y + b.y) / 2 - 2);
          ctx.textAlign = 'left';
        }
      });

      state.nodes.forEach(function(n){
        if (n._hidden) return;
        var r = n.r || (n.type === 'Entity' ? 8 : 12);
        var col = nodeColor(n);
        var hov = state.hov === n;
        var sel = state.selected && state.selected.id === n.id;
        var hl  = state.searchHighlight.has(n.id);
        var pulse = n._pulseUntil && n._pulseUntil > Date.now();
        ctx.shadowColor = col;
        ctx.shadowBlur = (hov || sel || hl || pulse) ? 16 : 5;
        if (pulse) {
          var t = (n._pulseUntil - Date.now()) / 1500;
          var rr = r + (1 - t) * 6;
          ctx.beginPath();
          ctx.arc(n.x, n.y, rr, 0, Math.PI * 2);
          ctx.strokeStyle = col + 'aa';
          ctx.lineWidth = 1.4;
          ctx.stroke();
        }
        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        ctx.fillStyle = col + ((hov || sel || hl) ? 'ff' : (n._alpha || 'aa'));  /* relevance opacity */
        ctx.fill();
        ctx.shadowBlur = 0;
        ctx.strokeStyle = (hov || sel || hl) ? '#fff' : col + '55';
        ctx.lineWidth   = (hov || sel || hl) ? 2 : 1;
        ctx.stroke();
        if (state.scale > 0.4) {
          ctx.font = '9px monospace';
          ctx.fillStyle = (hov || sel) ? '#fff' : '#ddd5c8bb';
          ctx.textAlign = 'center';
          ctx.fillText(String(n.label || n.id || '').slice(0, 24), n.x, n.y + r + 10);
          if (n.type && state.scale > 0.7) {
            ctx.font = '7px monospace';
            ctx.fillStyle = col + 'cc';
            var tlabel = n.type === 'Entity' && n.props && n.props.type ? n.props.type : n.type;
            ctx.fillText(String(tlabel).slice(0, 10), n.x, n.y + 3);
          }
          ctx.textAlign = 'left';
        }
        if (state.expanded[n.id] && state.expanded[n.id].length) {
          ctx.beginPath();
          ctx.arc(n.x + r * 0.7, n.y - r * 0.7, 3, 0, Math.PI * 2);
          ctx.fillStyle = '#5a9e8f';
          ctx.fill();
        }
      });
      ctx.restore();
    }

    // ── Force-directed layout ────────────────────────────────────────────
    function tick(){
      state.tickCount++;
      if (state.stopped) return;
      if (state.tickCount > 280 && !state.drag) {
        // One cooling pass has completed. If auto-anneal cycles remain, re-energise
        // (reset the counter so damping/velocity caps return to their high
        // starting values) instead of freezing — this is exactly what pressing
        // Re-layout does, so the layout keeps improving until it's used up its
        // cycles or the user intervenes. Then it freezes for good.
        if (state.settleCycles > 0) {
          state.settleCycles--;
          state.tickCount = 0;
        } else {
          state.frozen = true;
        }
      }
      if (state.frozen) {
        draw();
        state.animHandle = setTimeout(function(){
          if (state.stopped) return;
          state.animHandle = requestAnimationFrame(tick);
        }, 100);
        return;
      }
      var nm2 = {};
      state.nodes.forEach(function(n){ nm2[n.id] = n; });
      var damping = 0.85 - Math.min(0.4, state.tickCount / 700);
      var maxV = Math.max(0.5, 8 - state.tickCount * 0.02);

      // ── Force+Axis mode: the AXIS takes precedence ───────────────────────
      // Pure positional relaxation — NO velocity is accumulated, so the system
      // cannot oscillate (the previous velocity-spring version resonated, making
      // the whole cloud squash and expand). Each tick we move every node a small
      // fraction toward its axis target, plus a small bounded declutter offset
      // that nudges apart only nodes sitting almost on top of each other. Both
      // terms are pure position deltas applied once, so it converges and stops.
      if (state._axisMode) {
        var deltas = new Array(state.nodes.length);
        for (var ai = 0; ai < state.nodes.length; ai++) {
          var a = state.nodes[ai];
          var ddx = 0, ddy = 0;
          // bounded local declutter: only against very close neighbours
          for (var bi = 0; bi < state.nodes.length; bi++) {
            if (ai === bi) continue;
            var b = state.nodes[bi];
            var rx = a.x - b.x, ry = a.y - b.y;
            var r2 = rx * rx + ry * ry;
            if (r2 > 2600 || r2 < 0.001) continue;   // ~50px radius only
            var r = Math.sqrt(r2);
            var minD = (a.r || 9) + (b.r || 9) + 6;
            if (r < minD) {                          // overlapping → small fixed shove
              var s = (minD - r) * 0.5;
              ddx += (rx / r) * s; ddy += (ry / r) * s;
            }
          }
          // cap the declutter so it can never fling a node out of its axis band
          var dl = Math.sqrt(ddx * ddx + ddy * ddy);
          if (dl > 12) { ddx = ddx / dl * 12; ddy = ddy / dl * 12; }
          deltas[ai] = { dx: ddx, dy: ddy };
        }
        for (var pi = 0; pi < state.nodes.length; pi++) {
          var n = state.nodes[pi];
          n.vx = 0; n.vy = 0;                        // keep velocity zeroed (no carry-over)
          if (state.drag === n) continue;
          var tx = (n._axisX !== undefined) ? n._axisX : n.x;
          var ty = (n._axisY !== undefined) ? n._axisY : n.y;
          // move a fraction toward the axis target + the bounded declutter offset
          n.x += (tx - n.x) * 0.22 + deltas[pi].dx;
          n.y += (ty - n.y) * 0.22 + deltas[pi].dy;
        }
        draw();
        state.animHandle = requestAnimationFrame(tick);
        return;
      }

      // When a layer has physics disabled, its nodes are pinned (they still draw
      // and still exert forces on others, but don't move themselves) — useful
      // for freezing a structural backbone while the informational layer settles.
      var _anyPhysicsOff = state.layers && Object.keys(state.layers).some(function(k){ return state.layers[k].physics === false; });

      state.nodes.forEach(function(a){
        if (state.drag === a) { a.vx = 0; a.vy = 0; return; }
        if (_anyPhysicsOff && !_layerPhysics(a._layer)) { a.vx = 0; a.vy = 0; return; }
        for (var i = 0; i < state.nodes.length; i++) {
          var b = state.nodes[i];
          if (a === b) continue;
          var dx = a.x - b.x, dy = a.y - b.y;
          var d2 = dx * dx + dy * dy + 1;
          var d = Math.sqrt(d2);
          var f = Math.min(60, 1200 / d2);
          a.vx += f * dx / d;
          a.vy += f * dy / d;
        }
      });
      state.edges.forEach(function(e){
        var a = nm2[e.from], b = nm2[e.to];
        if (!a || !b) return;
        // Skip the spring entirely if the edge's layer has physics disabled.
        if (_anyPhysicsOff && e._layer && !_layerPhysics(e._layer)) return;
        var dx = b.x - a.x, dy = b.y - a.y;
        var d  = Math.sqrt(dx * dx + dy * dy) + 0.1;
        // Per-edge spring from edgeStyleFn
        var es = opts.edgeStyleFn ? opts.edgeStyleFn(e) : null;
        var restLen = (es && es.springLength) || 80;
        var str     = (es && es.springStrength) || 0.012;
        // Bridge-aware rest length: an edge touching a node that connects to
        // many clusters is stretched longer (so connectors sit out between the
        // groups they bridge); edges wholly inside one cluster stay short. We
        // take the larger bridge count of the two endpoints.
        var bridge = Math.max(a._bridge || 1, b._bridge || 1);
        if (bridge > 1) restLen += Math.min(220, (bridge - 1) * 70);
        // A cross-cluster edge (endpoints in different communities) also gets a
        // little extra length so distinct groups don't sit on top of each other.
        if (a._cluster !== undefined && a._cluster !== b._cluster) restLen += 60;
        var f  = str * (d - restLen);
        var aPinned = _anyPhysicsOff && !_layerPhysics(a._layer);
        var bPinned = _anyPhysicsOff && !_layerPhysics(b._layer);
        if (state.drag !== a && !aPinned) { a.vx += f * dx / d; a.vy += f * dy / d; }
        if (state.drag !== b && !bPinned) { b.vx -= f * dx / d; b.vy -= f * dy / d; }
      });

      // ── Cluster separation ───────────────────────────────────────────────
      // Push whole communities apart so they don't overlap or interlock. We
      // compute each cluster's centroid and node count, then apply an
      // inverse-distance repulsion between centroids, distributed to member
      // nodes. Scaled by cluster size so big groups claim more room.
      var cids = state._clusterIds;
      if (cids && cids.length > 1) {
        var cen = {};
        for (var ci = 0; ci < cids.length; ci++) cen[cids[ci]] = { x: 0, y: 0, n: 0 };
        for (var ni0 = 0; ni0 < state.nodes.length; ni0++) {
          var nn0 = state.nodes[ni0]; var c0 = cen[nn0._cluster];
          if (c0) { c0.x += nn0.x; c0.y += nn0.y; c0.n++; }
        }
        for (var ci2 = 0; ci2 < cids.length; ci2++) {
          var c = cen[cids[ci2]]; if (c.n) { c.x /= c.n; c.y /= c.n; }
        }
        // centroid-to-centroid repulsion vector per cluster
        var push = {};
        for (var a1 = 0; a1 < cids.length; a1++) {
          var ca = cen[cids[a1]]; push[cids[a1]] = push[cids[a1]] || { x: 0, y: 0 };
          for (var b1 = 0; b1 < cids.length; b1++) {
            if (a1 === b1) continue;
            var cb = cen[cids[b1]];
            var ddx = ca.x - cb.x, ddy = ca.y - cb.y;
            var dd2 = ddx * ddx + ddy * ddy + 1;
            var dd = Math.sqrt(dd2);
            // desired minimum gap grows with the two clusters' sizes
            var want = 160 + Math.sqrt(ca.n) * 26 + Math.sqrt(cb.n) * 26;
            // repel when closer than the wanted gap (prevents overlap/interlock)
            var force = 0;
            if (dd < want) force = (want - dd) * 0.015;       // strong anti-overlap
            force += Math.min(2.2, 9000 / dd2);                // gentle long-range push
            push[cids[a1]].x += (ddx / dd) * force;
            push[cids[a1]].y += (ddy / dd) * force;
          }
        }
        // apply each cluster's push to its member nodes
        for (var ni1 = 0; ni1 < state.nodes.length; ni1++) {
          var nn1 = state.nodes[ni1];
          if (state.drag === nn1) continue;
          var pu = push[nn1._cluster];
          if (pu) { nn1.vx += pu.x; nn1.vy += pu.y; }
        }
      }
      state.nodes.forEach(function(n){
        if (state.drag === n) return;
        if (_anyPhysicsOff && !_layerPhysics(n._layer)) { n.vx = 0; n.vy = 0; return; }
        n.vx *= damping; n.vy *= damping;
        var spd = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
        if (spd > maxV) {
          n.vx = (n.vx / spd) * maxV;
          n.vy = (n.vy / spd) * maxV;
        }
        var WB = W * 4, HB = H * 4;
        n.x = Math.max(-WB, Math.min(WB, n.x + n.vx));
        n.y = Math.max(-HB, Math.min(HB, n.y + n.vy));
      });
      draw();
      state.animHandle = requestAnimationFrame(tick);
    }
    function wake(){
      state.frozen = false;
      state.tickCount = 0;
      // Always restart the tick so resize() is called before the next draw().
      // If the canvas was hidden (W/H=0) when nodes were added, this corrects it.
      startTick();
    }
    function startTick(){
      if (state.animHandle) {
        try { cancelAnimationFrame(state.animHandle); } catch(e) {}
        try { clearTimeout(state.animHandle); } catch(e) {}
        state.animHandle = null;
      }
      state.stopped = false;
      // We need W and H to be non-zero before the first tick/draw.
      // If the parent panel is hidden (display:none), getBoundingClientRect()
      // returns 0 — even after a rAF — until the panel is made visible.
      // Poll via setTimeout until the canvas has real dimensions, then start.
      var _startAttempts = 0;
      function _attemptStart(){
        if (state.stopped) return;
        try { resize(); } catch(e) {}
        if (W && H) {
          // Canvas is visible and sized — begin the tick loop.
          tick();
        } else if (_startAttempts < 40) {
          // Still hidden — retry every 50ms (up to ~2 seconds).
          _startAttempts++;
          state.animHandle = setTimeout(_attemptStart, 50);
        }
        // If still not visible after 40 attempts, give up silently.
        // The visibility detection (_revive/MutationObserver/poll) will
        // restart the loop when the panel actually becomes visible.
      }
      state.animHandle = setTimeout(_attemptStart, 0);
    }
    function stopTick(){
      state.stopped = true;
      if (state.animHandle) {
        try { cancelAnimationFrame(state.animHandle); } catch(e) {}
        try { clearTimeout(state.animHandle); } catch(e) {}
      }
      state.animHandle = null;
    }

    // ── Re-show recovery ─────────────────────────────────────────────────
    // When the host panel is hidden (panel/tab switch) the canvas can stop
    // repainting and return blank. Whenever it becomes visible again, force a
    // full restart of the tick loop so the canvas is always re-rendered cleanly.
    //
    // Root cause: when frozen, the tick loop runs draw() on a 100ms
    // setTimeout→rAF cycle. Browsers pause/throttle rAF on hidden elements,
    // and the canvas backing store is often 0×0 while the panel is hidden.
    // On tab return the rAF fires before our visibility poll, calling draw()
    // against a stale/empty canvas. The fix is to always cancel+restart the
    // tick loop (which calls resize() first) rather than just nudging flags.
    function _revive(){
      try { resize(); } catch(e){}
      // Always restart the tick — this cancels any pending frozen rAF/timeout,
      // re-measures the canvas, and re-runs the simulation + draw cleanly.
      startTick();
    }

    // IntersectionObserver: catches element entering viewport after display:none
    // is removed. Use a very low threshold so even a 1px sliver triggers it.
    if (typeof IntersectionObserver !== 'undefined') {
      try {
        new IntersectionObserver(function(entries){
          for (var i = 0; i < entries.length; i++) {
            if (entries[i].isIntersecting) { _revive(); break; }
          }
        }, { threshold: [0, 0.01] }).observe(canvas);
      } catch(e){}
    }

    // document visibilitychange: catches browser tab / window switch
    try {
      document.addEventListener('visibilitychange', function(){
        if (!document.hidden) _revive();
      });
    } catch(e){}

    // MutationObserver on ancestor chain: catches harness panels toggling
    // display:none / display:block|flex on a parent element (the most common
    // pattern used by Vera's panel switcher).  We walk up to 12 levels and
    // watch for style / class attribute mutations — when any ancestor's
    // computed display flips from none to something, call _revive().
    try {
      var _mutObs = new MutationObserver(function(){
        // Re-check visibility: if canvas is now visible and wasn't before, revive.
        var vis = !!(canvas.offsetParent !== null && canvas.clientWidth > 0);
        if (vis && !_wasVisible) { _wasVisible = true; _revive(); }
      });
      var _ancestor = container.parentElement;
      for (var _ai = 0; _ai < 12 && _ancestor && _ancestor !== document.body; _ai++) {
        _mutObs.observe(_ancestor, { attributes: true, attributeFilter: ['style', 'class'] });
        _ancestor = _ancestor.parentElement;
      }
    } catch(e){}

    // Polling fallback: cheap 200ms poll. Catches any remaining edge cases
    // (e.g. canvas size drift, harnesses that don't mutate style attributes
    // but reparent nodes). Initialise _wasVisible to false so the first poll
    // always evaluates the current state correctly.
    var _wasVisible = false;
    try {
      setInterval(function(){
        if (!canvas) return;
        var vis = !!(canvas.offsetParent !== null && canvas.clientWidth > 0);
        if (vis && !_wasVisible) {
          // Transition: hidden → visible
          _revive();
        } else if (vis && canvas.clientWidth &&
                   Math.abs((canvas.width / (window.devicePixelRatio || 1)) - canvas.clientWidth) > 2) {
          // Canvas backing store size has drifted from its CSS size (e.g. after
          // a layout reflow while hidden) — resize + redraw.
          _revive();
        }
        _wasVisible = vis;
      }, 200);
    } catch(e){}

    // ── Mouse interactions ───────────────────────────────────────────────
    function s2w(sx, sy){ return { x: (sx - state.off.x) / state.scale, y: (sy - state.off.y) / state.scale }; }
    function findNode(sx, sy){
      var w = s2w(sx, sy);
      for (var i = state.nodes.length - 1; i >= 0; i--) {
        var n = state.nodes[i];
        if (n._hidden) continue;
        if (Math.hypot(n.x - w.x, n.y - w.y) < (n.r || 12) + 4) return n;
      }
      return null;
    }

    canvas.onmousedown = function(e){
      var rect = canvas.getBoundingClientRect();
      var mx = e.clientX - rect.left, my = e.clientY - rect.top;
      var n = findNode(mx, my);
      if (n) state.drag = n;
      else state.pan = { mx: mx, my: my, ox: state.off.x, oy: state.off.y };
    };
    canvas.onmousemove = function(e){
      var rect = canvas.getBoundingClientRect();
      var mx = e.clientX - rect.left, my = e.clientY - rect.top;
      if (state.drag) {
        var w = s2w(mx, my);
        state.drag.x = w.x; state.drag.y = w.y;
        state.drag.vx = 0; state.drag.vy = 0;
      } else if (state.pan) {
        state.off.x = state.pan.ox + (mx - state.pan.mx);
        state.off.y = state.pan.oy + (my - state.pan.my);
      } else {
        state.hov = findNode(mx, my);
        if (state.hov && tooltip) {
          var n = state.hov;
          var label = (n.label || n.id || '').slice(0, 60);
          var sub = n.type || 'Node';
          if (n.type === 'Entity' && n.props && n.props.type) sub = 'Entity · ' + n.props.type;
          if (n.props && n.props.mention_count) sub += ' · ' + n.props.mention_count + ' mentions';
          tooltip.style.display = 'block';
          tooltip.style.left = (mx + 12) + 'px';
          tooltip.style.top  = (my + 12) + 'px';
          tooltip.innerHTML = '<b>' + esc(label) + '</b><br><span style="color:var(--dim2);font-size:9px">' + esc(sub) + '</span><br><span style="color:var(--dim);font-size:8.5px">click for actions</span>';
        } else if (tooltip) {
          tooltip.style.display = 'none';
        }
      }
      canvas.style.cursor = (state.drag || state.hov) ? 'pointer' : (state.pan ? 'grabbing' : 'grab');
    };
    canvas.onmouseup = function(e){
      var clicked = state.drag;
      state.drag = null; state.pan = null;
      if (clicked && Math.abs(e.movementX) < 4 && Math.abs(e.movementY) < 4) {
        state.selected = clicked;
        if (opts.onNodeClick) {
          var rv = opts.onNodeClick(clicked, instance);
          if (rv === false) return; // host suppressed default
        }
        showDetail(clicked);
      }
    };
    canvas.ondblclick = function(e){
      var rect = canvas.getBoundingClientRect();
      var n = findNode(e.clientX - rect.left, e.clientY - rect.top);
      if (n) {
        e.preventDefault();
        if (opts.onNodeDblClick) {
          var rv = opts.onNodeDblClick(n, instance);
          if (rv === false) return;
        }
        if (state.expanded[n.id] && state.expanded[n.id].length) {
          collapseNode(n);
        } else {
          expandEntities(n);
        }
      }
    };
    canvas.oncontextmenu = function(e){
      var rect = canvas.getBoundingClientRect();
      var n = findNode(e.clientX - rect.left, e.clientY - rect.top);
      if (n) {
        e.preventDefault();
        showDetail(n);
        return false;
      }
    };
    canvas.onmouseleave = function(){
      if (tooltip) tooltip.style.display = 'none';
    };
    canvas.onwheel = function(e){
      e.preventDefault();
      var rect = canvas.getBoundingClientRect();
      var mx = e.clientX - rect.left, my = e.clientY - rect.top;
      var f = e.deltaY < 0 ? 1.11 : 0.9;
      state.off.x = mx - (mx - state.off.x) * f;
      state.off.y = my - (my - state.off.y) * f;
      state.scale = Math.max(0.12, Math.min(5, state.scale * f));
    };

    // ── Search (two modes) ──────────────────────────────────────────────────
    // Mode "list":      shows a results dropdown; clicking a result pans+zooms
    //                   so that node is nicely centred and selected.
    // Mode "highlight": highlights every matching node PLUS its connected
    //                   neighbours out to a selectable depth (0–3 hops), and
    //                   dims the rest.
    var searchModeEl  = container.querySelector('.vg-search-mode');
    var searchDepthEl = container.querySelector('.vg-search-depth');
    var searchResEl   = container.querySelector('.vg-search-results');

    function _searchMatches(q){
      q = (q || '').trim().toLowerCase();
      if (!q) return [];
      var out = [];
      state.nodes.forEach(function(n){
        var l = String(n.label || '').toLowerCase();
        var i = String(n.id || '').toLowerCase();
        var name = (n.props && n.props.name) ? String(n.props.name).toLowerCase() : '';
        if (l.indexOf(q) >= 0 || i.indexOf(q) >= 0 || name.indexOf(q) >= 0) out.push(n);
      });
      return out;
    }

    // Build an adjacency map once for neighbour expansion.
    function _neighboursWithin(seedIds, depth){
      var adj = {};
      state.edges.forEach(function(e){
        (adj[e.from] || (adj[e.from] = [])).push(e.to);
        (adj[e.to]   || (adj[e.to]   = [])).push(e.from);
      });
      var seen = {};
      seedIds.forEach(function(id){ seen[id] = 0; });
      var frontier = seedIds.slice();
      for (var d = 0; d < depth; d++) {
        var next = [];
        frontier.forEach(function(id){
          (adj[id] || []).forEach(function(nb){
            if (seen[nb] === undefined) { seen[nb] = d + 1; next.push(nb); }
          });
        });
        frontier = next;
        if (!frontier.length) break;
      }
      return seen;   // id -> hop distance
    }

    function _panZoomTo(node){
      if (!node) return;
      // nicely centre the node at a comfortable zoom
      state.scale = Math.max(state.scale, 1.1);
      state.off.x = W / 2 - node.x * state.scale;
      state.off.y = H / 2 - node.y * state.scale;
      state.selected = node;
      if (typeof showDetail === 'function') { try { showDetail(node); } catch(e){} }
      draw();
    }

    function _renderSearchResults(matches){
      if (!searchResEl) return;
      if (!matches.length) { searchResEl.style.display = 'none'; searchResEl.innerHTML = ''; return; }
      searchResEl.style.display = '';
      searchResEl.innerHTML = matches.slice(0, 60).map(function(n, i){
        var sub = n.type || (n.props && n.props.type) || '';
        return '<div class="vg-sr-row" data-id="'+esc(n.id)+'" style="padding:4px 8px;cursor:pointer;' +
          'border-bottom:1px solid var(--border,#3a3530);font-size:10px;display:flex;gap:6px;align-items:center">' +
          '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text,#ddd5c8)">'+esc(n.label||n.id)+'</span>' +
          '<span style="font-size:8px;color:var(--dim,#6a6058);font-family:var(--mono,monospace)">'+esc(String(sub).slice(0,14))+'</span>' +
          '</div>';
      }).join('');
      searchResEl.querySelectorAll('.vg-sr-row').forEach(function(row){
        row.onmouseenter = function(){ row.style.background = 'var(--bg2,#272421)'; };
        row.onmouseleave = function(){ row.style.background = ''; };
        row.onclick = function(){
          var id = row.getAttribute('data-id');
          var node = state.nodeIndex[id] || state.nodes.find(function(n){ return n.id === id; });
          // highlight just this node and centre it
          state.searchHighlight = new Set([id]);
          _panZoomTo(node);
          searchResEl.style.display = 'none';
        };
      });
    }

    function _runSearch(){
      if (!searchEl) return;
      var q = (searchEl.value || '').trim();
      var mode = searchModeEl ? searchModeEl.getAttribute('data-mode') : 'list';
      if (!q) {
        state.searchHighlight = new Set();
        if (searchResEl) { searchResEl.style.display = 'none'; searchResEl.innerHTML = ''; }
        draw();
        return;
      }
      var matches = _searchMatches(q);
      if (mode === 'highlight') {
        if (searchResEl) searchResEl.style.display = 'none';
        var depth = searchDepthEl ? parseInt(searchDepthEl.value || '1') : 1;
        var ids = matches.map(function(n){ return n.id; });
        var within = _neighboursWithin(ids, depth);
        state.searchHighlight = new Set(Object.keys(within));
        // centre on the matches' centroid so the highlighted region is in view
        if (matches.length) {
          var mx = 0, my = 0; matches.forEach(function(n){ mx += n.x; my += n.y; });
          mx /= matches.length; my /= matches.length;
          state.off.x = W / 2 - mx * state.scale;
          state.off.y = H / 2 - my * state.scale;
        }
        draw();
      } else {
        // list mode — show results dropdown; highlight matches lightly
        state.searchHighlight = new Set(matches.map(function(n){ return n.id; }));
        _renderSearchResults(matches);
        draw();
      }
    }

    if (searchEl) {
      searchEl.addEventListener('input', throttle(_runSearch, 150));
      searchEl.addEventListener('focus', function(){
        if (searchModeEl && searchModeEl.getAttribute('data-mode') === 'list' && searchEl.value.trim()) _runSearch();
      });
      // Enter in list mode jumps to the first result
      searchEl.addEventListener('keydown', function(ev){
        if (ev.key === 'Enter') {
          var m = _searchMatches(searchEl.value);
          if (m.length) { state.searchHighlight = new Set([m[0].id]); _panZoomTo(m[0]); if (searchResEl) searchResEl.style.display='none'; }
        } else if (ev.key === 'Escape') {
          searchEl.value = ''; _runSearch();
        }
      });
    }
    if (searchModeEl) {
      searchModeEl.onclick = function(){
        var m = searchModeEl.getAttribute('data-mode') === 'list' ? 'highlight' : 'list';
        searchModeEl.setAttribute('data-mode', m);
        searchModeEl.textContent = (m === 'list') ? 'List' : 'Highlight';
        searchModeEl.title = (m === 'list')
          ? 'Search mode: List + zoom to result. Click to switch to Highlight.'
          : 'Search mode: Highlight matches + neighbours to depth. Click to switch to List.';
        if (searchDepthEl) searchDepthEl.style.display = (m === 'highlight') ? '' : 'none';
        if (searchResEl) searchResEl.style.display = 'none';
        _runSearch();
      };
    }
    if (searchDepthEl) searchDepthEl.onchange = _runSearch;
    // hide the results dropdown when clicking elsewhere
    if (canvas) canvas.addEventListener('mousedown', function(){ if (searchResEl) searchResEl.style.display = 'none'; });

    if (relayoutBtn) relayoutBtn.onclick = function(){
      // One press = a few automatic anneal cycles, so a single click does what
      // previously took several. (You can still click again to keep going.)
      state.settleCycles = state.nodes.length > 120 ? 5 : 3;
      wake();
    };
    if (fitBtn) fitBtn.onclick = function(){
      if (!state.nodes.length) return;
      var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
      state.nodes.forEach(function(n){
        if (n.x < minX) minX = n.x; if (n.x > maxX) maxX = n.x;
        if (n.y < minY) minY = n.y; if (n.y > maxY) maxY = n.y;
      });
      var pad = 60;
      var graphW = (maxX - minX) + pad * 2;
      var graphH = (maxY - minY) + pad * 2;
      var sx = W / graphW, sy = H / graphH;
      state.scale = Math.min(2, Math.max(0.15, Math.min(sx, sy)));
      state.off.x = W / 2 - ((minX + maxX) / 2) * state.scale;
      state.off.y = H / 2 - ((minY + maxY) / 2) * state.scale;
    };

    if (layerEl) {
      layerEl.addEventListener('change', function(){
        state.currentLayer = layerEl.value;
        instance.fetchSnapshot(state.currentLayer, opts.layerOpts || {});
      });
    }

    // ════════════════════════════════════════════════════════════════════
    // LEFT PANEL WIRING
    // ════════════════════════════════════════════════════════════════════
    var _leftEl = container.querySelector('.vg-left');
    var _typeChipEl = container.querySelector('.vg-type-chips');
    var _edgeChipEl = container.querySelector('.vg-edge-chips');
    var _typeCntEl = container.querySelector('.vg-type-cnt');
    var _edgeCntEl = container.querySelector('.vg-edge-cnt');
    var _debugEl = container.querySelector('.vg-debug');
    var _statBarEl = container.querySelector('.vg-stat-bar');
    var _sessSecEl = container.querySelector('.vg-sessions-sec');
    var _spListEl = container.querySelector('.vg-sp-list');
    var _memModeEl = container.querySelector('.vg-mem-mode');
    var _memSelEl = container.querySelector('.vg-mem-sel');
    var _entChipEl = container.querySelector('.vg-ent-chips');
    var _entCntEl  = container.querySelector('.vg-ent-cnt');
    var _entSlEl   = container.querySelector('.vg-ent-sl');
    var _relSlider = container.querySelector('.vg-rel-slider');
    var _relValEl  = container.querySelector('.vg-rel-val');
    var _relSlEl   = container.querySelector('.vg-rel-sl');
    var _relWrapEl = container.querySelector('.vg-rel-wrap');
    var _typeOff = new Set(), _edgeOff = new Set();
    var _typeIn = new Set(), _edgeIn = new Set();   // include-mode selections
    var _filterMode = 'exclude';                     // 'exclude' | 'include'
    var _minRel = 0;
    var _layoutMode = 'default';
    var _sessions = [], _selectedSids = new Set();

    // ── Entity classification (so entity subtypes get their own filter group) ─
    // Node types treated as entities are split into the "Entities" chip group
    // and keyed by their subtype. Everything else stays under "Node Types".
    var ENTITY_SUBTYPES = {
      person:1, organisation:1, organization:1, location:1, place:1,
      date:1, time:1, year:1, technology:1, product:1, concept:1,
      'function':1, 'class':1, module:1, type_name:1, constant:1,
      domain:1, account:1, email:1, identity:1, named_entity:1,
      event:1, money:1, role:1, title:1
    };
    function _isEntity(n){ return !!n && (n.type === 'Entity' || !!ENTITY_SUBTYPES[n.type]); }
    function _entKey(n){
      if (n && n.type === 'Entity' && n.props && n.props.type) return n.props.type;
      return (n && n.type) || '?';
    }
    function _typeKey(n){ return _isEntity(n) ? _entKey(n) : ((n && n.type) || '?'); }
    function _isRoot(n){ return !!n && (n.type === 'Dataset' || (n.props && n.props.root)); }
    function _nodeRel(n){
      var p = (n && n.props) || {};
      return (typeof p.relevance === 'number') ? Math.max(0, Math.min(1, p.relevance)) : 0.5;
    }
    function _relAlpha(rel){
      var a = Math.round(60 + Math.max(0, Math.min(1, rel)) * 195); // 60..255
      var h = a.toString(16); return (h.length < 2 ? '0' + h : h);
    }
    // Relevance -> opacity. Low-relevance nodes are lighter (never hidden). The
    // slider (minRel) deepens the fade of nodes below the threshold, floored so
    // they always stay faintly visible.
    function _relFade(rel, minRel, isRoot){
      rel = Math.max(0, Math.min(1, rel));
      var a = 0.20 + 0.80 * rel;
      if (!isRoot && minRel > 0 && rel < minRel) {
        var deficit = (minRel - rel) / minRel;
        a *= (1 - 0.80 * deficit);
      }
      a = Math.max(0.12, Math.min(1, a));
      var h = Math.round(a * 255).toString(16);
      return h.length < 2 ? '0' + h : h;
    }
    function _chipColor(key, isEnt){
      try { return isEnt ? nodeColor({type:'Entity', props:{type:key}}) : nodeColor({type:key}); }
      catch(e){ return '#888'; }
    }
    function _chipHTML(key, count, color, on){
      return '<span class="vg-chip'+(on?' on':'')+'" data-t="'+esc(key)+'"><i style="background:'+color+'"></i>'+esc(key)+'<span class="cc">'+count+'</span></span>';
    }
    // include/exclude switch — whether a chip is "on" (and its kind visible):
    //   exclude: on unless its key is in the off-set (click a chip to hide that kind)
    //   include: on only if its key is in the in-set (click to show only those);
    //            an empty in-set means "show everything" until a chip is picked.
    function _chipOn(key, isEdge){
      if (_filterMode === 'include') {
        var s = isEdge ? _edgeIn : _typeIn;
        return s.size === 0 ? true : s.has(key);
      }
      return !(isEdge ? _edgeOff : _typeOff).has(key);
    }
    function _typeHidden(key){
      if (_filterMode === 'include') return _typeIn.size > 0 && !_typeIn.has(key);
      return _typeOff.size > 0 && _typeOff.has(key);
    }
    function _edgeHidden(rel){
      rel = rel || 'RELATED';
      if (_filterMode === 'include') return _edgeIn.size > 0 && !_edgeIn.has(rel);
      return _edgeOff.size > 0 && _edgeOff.has(rel);
    }

    // ── Collapse toggle ─────────────────────────────────────────────────
    var _leftToggle = container.querySelector('.vg-left-toggle');
    if (_leftToggle && _leftEl) {
      _leftToggle.addEventListener('click', function(){
        _leftEl.classList.toggle('collapsed');
      });
    }

    // ── Tag chips ────────────────────────────────────────────────────────
    var _tagChipEl = container.querySelector('.vg-tag-chips');
    function _rebuildTags(){
      if (!_tagChipEl) return;
      var tags = {};
      state.nodes.forEach(function(n){
        if (n._hidden) return;
        var p = n.props || {};
        var t = p.tags || p.category || '';
        if (typeof t === 'string' && t) t.split(',').forEach(function(s){ var v=s.trim(); if(v) tags[v]=(tags[v]||0)+1; });
        if (Array.isArray(t)) t.forEach(function(v){ if(v) tags[v]=(tags[v]||0)+1; });
      });
      var sorted = Object.entries(tags).sort(function(a,b){return b[1]-a[1];}).slice(0,30);
      _tagChipEl.innerHTML = sorted.length ? sorted.map(function(e){
        return '<span class="vg-chip on" data-tag="'+esc(e[0])+'">'+esc(e[0])+'<span class="cc">'+e[1]+'</span></span>';
      }).join('') : '<span style="font-size:8.5px;color:var(--dim,#6a6058)">No tags</span>';
    }

    // ── Graph source picker ─────────────────────────────────────────────
    var _srcBtns = container.querySelectorAll('.vg-gp .tb');
    _srcBtns.forEach(function(btn){
      btn.addEventListener('click', function(){
        _srcBtns.forEach(function(b){b.classList.remove('on');});
        btn.classList.add('on');
        var src = btn.dataset.src;
        if (_memModeEl) _memModeEl.style.display = src === 'memory' ? '' : 'none';
        if (_sessSecEl) _sessSecEl.style.display = src === 'memory' ? '' : 'none';
        if (src === 'memory') {
          var mmode = _memSelEl ? _memSelEl.value : 'session';
          _loadMemory(mmode);
        } else {
          var labelMap = {fabric:'Dataset,Source,Category,Skill,Ontology,Agent,DAG', net:'NetHost,SshHost,Subnet,NetService,Container,DockerHost'};
          instance.fetchSnapshot(src, {label_filter: labelMap[src] || ''});
        }
      });
    });
    if (_memSelEl) _memSelEl.addEventListener('change', function(){ _loadMemory(_memSelEl.value); });

    async function _loadMemory(mmode){
      var sid = '';
      try { sid = window.parent._veraSessionId || window.parent._chatSessionId || ''; } catch(e){}
      await instance.fetchMemory(mmode, {session_id: mmode==='session'?sid:'', hours:24});
    }

    // ── Layer toggle UI ─────────────────────────────────────────────────
    // Renders the per-layer visibility (eye) + physics (bolt) toggles. Prefers
    // the left-hand menu's "Layers" section; if the graph was created without a
    // left menu (e.g. the discovery panel embeds a bare graph), it falls back to
    // a small floating panel so the controls are still available everywhere.
    var _layerFloatEl = null;
    function _renderLayerUI(){
      if (opts.layerUI === false) return;
      var layers = Object.keys(state.layers || {});
      var nc = {};
      state.nodes.forEach(function(n){ nc[n._layer] = (nc[n._layer]||0)+1; });
      var meaningful = layers.filter(function(l){ return l !== 'default' || layers.length > 1; });

      var listEl = container.querySelector('.vg-layers-list');
      var slEl   = container.querySelector('.vg-layers-sl');

      function rowHTML(l){
        var rec = state.layers[l];
        var vis = rec.visible !== false, phys = rec.physics !== false;
        return '<div style="display:flex;align-items:center;gap:5px;padding:2px 3px;font-size:9px;font-family:var(--mono,monospace)">' +
          '<span class="vg-ly-eye" data-l="'+esc(l)+'" title="Toggle visibility" ' +
            'style="cursor:pointer;width:13px;text-align:center;color:'+(vis?'var(--acc2,#8fb87a)':'var(--dim,#6a6058)')+'">'+(vis?'\u25c9':'\u25cb')+'</span>' +
          '<span class="vg-ly-phys" data-l="'+esc(l)+'" title="Toggle physics" ' +
            'style="cursor:pointer;width:13px;text-align:center;color:'+(phys?'var(--acc,#5a9e8f)':'var(--dim,#6a6058)')+'">'+(phys?'\u26a1':'\u25ab')+'</span>' +
          '<span style="flex:1;color:var(--text,#ddd5c8)'+(vis?'':';opacity:.5')+';overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(l)+'</span>' +
          '<span style="color:var(--dim,#6a6058)">'+(nc[l]||0)+'</span>' +
          '</div>';
      }
      function wireToggles(root){
        root.querySelectorAll('.vg-ly-eye').forEach(function(el){
          el.onclick = function(){ var l=el.getAttribute('data-l'); instance.setLayerVisible(l, !(state.layers[l].visible!==false)); };
        });
        root.querySelectorAll('.vg-ly-phys').forEach(function(el){
          el.onclick = function(){ var l=el.getAttribute('data-l'); instance.setLayerPhysics(l, !(state.layers[l].physics!==false)); };
        });
      }

      if (listEl) {
        // Preferred: render into the left-hand menu.
        if (meaningful.length <= 1) {
          listEl.style.display = 'none';
          if (slEl) slEl.style.display = 'none';
          return;
        }
        listEl.style.display = '';
        if (slEl) slEl.style.display = '';
        listEl.innerHTML = meaningful.sort().map(rowHTML).join('');
        wireToggles(listEl);
        return;
      }

      // Fallback: no LHM present (bare graph) → floating panel over the canvas.
      if (meaningful.length <= 1) { if (_layerFloatEl) _layerFloatEl.style.display = 'none'; return; }
      if (!_layerFloatEl) {
        _layerFloatEl = document.createElement('div');
        _layerFloatEl.className = 'vg-layers-float';
        _layerFloatEl.style.cssText = 'position:absolute;top:8px;right:8px;z-index:20;' +
          'background:var(--bg1,#1f1d1a);border:1px solid var(--border,#3a3530);' +
          'border-radius:4px;padding:5px 7px;font-family:var(--mono,monospace);' +
          'font-size:9px;min-width:130px;box-shadow:0 2px 8px rgba(0,0,0,.4)';
        var host = container.querySelector('.vg-canvas-area') || container;
        if (host.style && !host.style.position) host.style.position = 'relative';
        host.appendChild(_layerFloatEl);
      }
      _layerFloatEl.style.display = '';
      _layerFloatEl.innerHTML =
        '<div style="color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.6px;' +
        'font-size:8px;margin-bottom:3px;display:flex;justify-content:space-between">' +
        '<span>Layers</span><span title="eye = show/hide, bolt = physics">\u25c9 \u26a1</span></div>' +
        meaningful.sort().map(rowHTML).join('');
      wireToggles(_layerFloatEl);
    }

    // ── Filter chip rebuild ─────────────────────────────────────────────
    function _rebuildChips(){
      if (!_typeChipEl || !_edgeChipEl) return;
      var tc={}, ec={}, entc={};
      state.nodes.forEach(function(n){
        if (_isEntity(n)) { var k=_entKey(n)||'?'; entc[k]=(entc[k]||0)+1; }
        else { var t=n.type||'?'; tc[t]=(tc[t]||0)+1; }
      });
      state.edges.forEach(function(e){ var r=e.rel||'RELATED'; ec[r]=(ec[r]||0)+1; });
      _typeChipEl.innerHTML = Object.keys(tc).sort().map(function(t){
        return _chipHTML(t, tc[t], _chipColor(t,false), _chipOn(t,false));
      }).join('');
      // entity subtype chips
      if (_entChipEl) {
        var ekeys = Object.keys(entc).sort();
        _entChipEl.innerHTML = ekeys.map(function(t){
          return _chipHTML(t, entc[t], _chipColor(t,true), _chipOn(t,false));
        }).join('');
        var hasEnt = ekeys.length > 0;
        _entChipEl.style.display = hasEnt ? '' : 'none';
        if (_entSlEl) _entSlEl.style.display = hasEnt ? '' : 'none';
        if (_entCntEl) _entCntEl.textContent = hasEnt ? ('('+ekeys.length+')') : '';
      }
      _edgeChipEl.innerHTML = Object.keys(ec).sort().map(function(r){
        return '<span class="vg-chip'+(_chipOn(r,true)?' on':'')+'" data-e="'+esc(r)+'"><i style="background:'+edgeColor(r)+'"></i>'+esc(r)+'<span class="cc">'+ec[r]+'</span></span>';
      }).join('');
      if (_typeCntEl) _typeCntEl.textContent = Object.keys(tc).length ? '('+Object.keys(tc).length+')' : '';
      if (_edgeCntEl) _edgeCntEl.textContent = Object.keys(ec).length ? '('+Object.keys(ec).length+')' : '';
      // relevance slider — shown when forced (opts.showRelevance) or any node
      // actually carries a numeric relevance score
      if (_relSlEl) {
        var showRel = opts.showRelevance === true ||
          state.nodes.some(function(n){ return n.props && typeof n.props.relevance === 'number'; });
        _relSlEl.style.display = showRel ? '' : 'none';
        if (_relWrapEl) _relWrapEl.style.display = showRel ? '' : 'none';
        if (showRel && _relValEl && !_relValEl.textContent) _relValEl.textContent = _minRel.toFixed(2);
      }
    }
    function _toggleNodeChip(c){
      var t=c.dataset.t;
      if (_filterMode === 'include') { if (_typeIn.has(t)) _typeIn.delete(t); else _typeIn.add(t); }
      else { if (_typeOff.has(t)) _typeOff.delete(t); else _typeOff.add(t); }
      _rebuildChips(); _applyVis(); draw();   // rebuild so "show all" state reflects across chips
    }
    if (_typeChipEl) _typeChipEl.addEventListener('click', function(ev){
      var c=ev.target.closest('.vg-chip'); if(!c) return; _toggleNodeChip(c);
    });
    if (_entChipEl) _entChipEl.addEventListener('click', function(ev){
      var c=ev.target.closest('.vg-chip'); if(!c) return; _toggleNodeChip(c);
    });
    if (_edgeChipEl) _edgeChipEl.addEventListener('click', function(ev){
      var c=ev.target.closest('.vg-chip'); if(!c) return;
      var r=c.dataset.e;
      if (_filterMode === 'include') { if (_edgeIn.has(r)) _edgeIn.delete(r); else _edgeIn.add(r); }
      else { if (_edgeOff.has(r)) _edgeOff.delete(r); else _edgeOff.add(r); }
      _rebuildChips(); _applyVis(); draw();
    });
    // include/exclude mode switch
    var _filterModeBtn = container.querySelector('.vg-filter-mode');
    if (_filterModeBtn) _filterModeBtn.addEventListener('click', function(){
      _filterMode = (_filterMode === 'exclude') ? 'include' : 'exclude';
      _filterModeBtn.textContent = (_filterMode === 'exclude') ? 'Exclude' : 'Include';
      _filterModeBtn.dataset.mode = _filterMode;
      _filterModeBtn.classList.toggle('on', _filterMode === 'include');
      _typeOff.clear(); _typeIn.clear(); _edgeOff.clear(); _edgeIn.clear();
      _rebuildChips(); _applyVis(); draw();
    });
    // relevance slider — fades/hides weak nodes WITHOUT relayout (positions stay)
    if (_relSlider) _relSlider.addEventListener('input', function(){
      _minRel = (parseInt(_relSlider.value, 10) || 0) / 100;
      if (_relValEl) _relValEl.textContent = _minRel.toFixed(2);
      _applyVis(); draw();
    });

    function _applyVis(){
      var relMode = opts.showRelevance === true ||
        state.nodes.some(function(n){ return n.props && typeof n.props.relevance === 'number'; });
      state.nodes.forEach(function(n){
        // Visibility is governed by type/entity filters AND layer visibility.
        // Relevance never hides a node — weaker nodes simply get lighter.
        n._hidden = _typeHidden(_typeKey(n)) || !_layerVisible(n._layer);
        if (relMode) {
          var rel = _isRoot(n) ? 1 : _nodeRel(n);
          n._alpha = _relFade(rel, _minRel, _isRoot(n));
        } else {
          n._alpha = undefined;
        }
      });
      // Edge filter for the draw loop (respects include/exclude mode)
      state._edgeOff = _edgeOff;
      state._edgeHidden = _edgeHidden;
    }

    // ── Update debug/stats ──────────────────────────────────────────────
    function _updateDebug(){
      var vis = state.nodes.filter(function(n){return !n._hidden;}).length;
      if (_debugEl) _debugEl.innerHTML = 'nodes: '+state.nodes.length+'<br>edges: '+state.edges.length+'<br>visible: '+vis;
      if (_statBarEl) _statBarEl.textContent = state.nodes.length+'n '+state.edges.length+'e';
    }

    // ── View mode switching ─────────────────────────────────────────────
    var _viewChips = container.querySelectorAll('.vg-view-chip');
    _viewChips.forEach(function(vc){
      vc.addEventListener('click', function(){
        _viewChips.forEach(function(c){c.classList.remove('on');});
        vc.classList.add('on');
        var mode = vc.dataset.layout;
        _layoutMode = mode;
        // Show/hide per-layout controls
        container.querySelectorAll('.vg-layout-ctrls > div').forEach(function(d){d.style.display='none';});
        var ctrl = container.querySelector('.vg-lc-'+mode);
        if (ctrl) ctrl.style.display = '';
        _applyLayout(mode);
      });
    });
    // Bind sliders to relayout
    container.querySelectorAll('.vg-layout-ctrls input, .vg-layout-ctrls select').forEach(function(el){
      el.addEventListener('input', function(){ if (_layoutMode !== 'default') _applyLayout(_layoutMode); });
      el.addEventListener('change', function(){ if (_layoutMode !== 'default') _applyLayout(_layoutMode); });
    });

    function _applyLayout(mode){
      var vis = state.nodes.filter(function(n){return !n._hidden;});
      if (!vis.length) return;

      if (mode === 'default') {
        state._axisMode = false;
        state.frozen = false; wake(); return;
      }

      // ── Latent map (static) ───────────────────────────────────────────────
      // Positions are supplied externally (by the WorldView system via
      // instance.setLatentMap()). Nodes are placed at their latent coordinates
      // and the simulation is frozen so nothing moves — this is a faithful,
      // static rendering of the latent space. No force, no drift.
      if (mode === 'latent-map') {
        state._axisMode = false;
        var lm = state._latentMap || {};
        vis.forEach(function(n){
          var p = lm[n.id];
          if (p) { n.x = p.x; n.y = p.y; }
          n.vx = 0; n.vy = 0;
        });
        state.frozen = true;
        // fit so the whole map is in view
        if (fitBtn) { try { fitBtn.onclick(); } catch(e){} }
        draw();
        return;
      }

      // Time calibration
      var tMin=Infinity,tMax=-Infinity,_haveTime=false;
      vis.forEach(function(n){
        var ts=n.props&&(n.props.created_at||n.props.timestamp);
        if(ts){var t=new Date(ts).getTime();if(Number.isFinite(t)){_haveTime=true;if(t<tMin)tMin=t;if(t>tMax)tMax=t;}}
      });
      if(!_haveTime){ tMin=0; tMax=0; }   // no timestamps anywhere — avoid Infinity/NaN
      var tRange=Math.max(tMax-tMin,1);
      function _axVal(n,ax){
        var p=n.props||{};
        if(ax==='time'){var ts=p.created_at||p.timestamp;if(!ts)return 6;var t=new Date(ts).getTime();return Math.sqrt(Math.max(0,(t-tMin)/tRange))*14;}
        if(ax==='importance')return parseFloat(p.importance||0.5)*12;
        if(ax==='source'){var sm={human:0,ai:2,tool:4,system:6,sensor:8,document:10};return sm[p.source_type]!==undefined?sm[p.source_type]:5;}
        if(ax==='category'){var h=0;var s=p.category||'';for(var i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))&0xffff;return h%13;}
        if(ax==='type'){var tm={session:0,Session:0,message:2,Memory:3,event:4,observation:5,Dataset:6,Source:7,dag:8,Entity:9,FabricRecord:10,fact:11,summary:12};return tm[n.type]!==undefined?tm[n.type]:6;}
        if(ax==='session'){var h2=0;var s2=p.session_id||n.id||'';for(var i2=0;i2<s2.length;i2++)h2=(h2*31+s2.charCodeAt(i2))&0xffff;return h2%13;}
        // ── graph-derived axes (work on ANY graph, no special props needed) ──
        if(ax==='cluster'){ // map each cluster id to a stable slot
          var cl=n._cluster||''; var ci=(state._clusterIds||[]).indexOf(cl);
          return ci>=0?ci:0;
        }
        if(ax==='layer'){ // structural vs informational vs default …
          var ls=Object.keys(state.layers||{}).sort(); var li=ls.indexOf(n._layer);
          return li>=0?li*3:0;   // spread layers a bit further apart
        }
        if(ax==='degree'){ // connection count (sqrt-compressed so hubs don't run off)
          return Math.sqrt(n._axisDeg||0)*3;
        }
        if(ax==='label'){ // alphabetical by label — gives a deterministic spread
          var s3=(n.label||n.id||'').toLowerCase(); var hv=0;
          for(var i3=0;i3<Math.min(s3.length,4);i3++)hv=hv*26+(s3.charCodeAt(i3)-97);
          return (hv%140)/10;
        }
        return 0;
      }
      // degree for the 'degree' axis
      (function(){
        var deg={}; state.edges.forEach(function(e){deg[e.from]=(deg[e.from]||0)+1;deg[e.to]=(deg[e.to]||0)+1;});
        vis.forEach(function(n){ n._axisDeg = deg[n.id]||0; });
      })();

      if (mode === 'force-axis') {
        var axX = (container.querySelector('.vg-ax-x')||{}).value || 'type';
        var axY = (container.querySelector('.vg-ax-y')||{}).value || 'cluster';
        var sp = parseFloat((container.querySelector('.vg-spread')||{}).value || 200);
        // Many nodes share the same axis values (e.g. same type => same Y), so
        // they'd all target the EXACT same point and pile up / jiggle. To avoid
        // that we bucket nodes by their (axX,axY) zone and lay each bucket out
        // in a small grid around the zone centre — the TARGET itself is spread,
        // so the declutter physics barely has to do anything.
        var buckets = {};
        vis.forEach(function(n){
          var zx = _axVal(n,axX), zy = _axVal(n,axY);
          var key = zx + '|' + zy;
          (buckets[key] || (buckets[key] = { zx: zx, zy: zy, nodes: [] })).nodes.push(n);
        });
        // cell size for the in-zone grid; keep it comfortably bigger than node radius
        var cell = 34;
        Object.keys(buckets).forEach(function(key){
          var bk = buckets[key];
          var baseX = bk.zx * sp, baseY = bk.zy * sp * 0.65;
          var m = bk.nodes.length;
          var cols = Math.max(1, Math.ceil(Math.sqrt(m)));
          // centre the grid block on the zone point
          var halfW = (cols - 1) * cell / 2;
          var rows = Math.ceil(m / cols);
          var halfH = (rows - 1) * cell / 2;
          bk.nodes.forEach(function(n, i){
            var cx2 = (i % cols) * cell - halfW;
            var cy2 = Math.floor(i / cols) * cell - halfH;
            n._axisX = baseX + cx2;
            n._axisY = baseY + cy2;
            n.x = n._axisX + (Math.random()-0.5)*6;
            n.y = n._axisY + (Math.random()-0.5)*6;
            n.vx=0; n.vy=0;
          });
        });
        state._axisMode = true;
        state.frozen = false; wake();

      } else if (mode === 'timeline') {
        var laneH = parseFloat((container.querySelector('.vg-tl-lane')||{}).value || 80);
        var pxH = parseFloat((container.querySelector('.vg-tl-scale')||{}).value || 60);
        var typeOrder = ['Session','session','message','Memory','event','observation','Dataset','Source','Entity','dag','FabricRecord','fact','summary'];
        var _laneCursor = {};   // per-lane counter (used for ordering + Y stagger)
        vis.forEach(function(n){
          var lane=typeOrder.indexOf(n.type); if(lane<0)lane=typeOrder.length;
          var ts=n.props&&(n.props.created_at||n.props.timestamp);
          var tv=ts?new Date(ts).getTime():NaN;
          var seq = (_laneCursor[lane] = (_laneCursor[lane]||0) + 1);
          if(_haveTime && Number.isFinite(tv)){
            n.x = ((tv-tMin)/3600000)*pxH;
          } else {
            // no usable timestamp → lay the node out along its lane by order so
            // it's visible (timeline degrades to a typed swim-lane layout).
            n.x = seq * Math.max(40, pxH);
          }
          // Stagger Y WITHIN the lane so nodes don't all sit on one horizontal
          // line (which makes edges between them colinear and unreadable). We
          // offset by a bounded zig-zag that stays inside the lane's band.
          var band = laneH * 0.6;                         // keep within the lane
          var stagger = ((seq % 5) - 2) / 2 * (band / 2); // -band/2 .. +band/2 in steps
          n.y = lane*laneH + stagger;
          n.vx=0; n.vy=0;
        });
        state.frozen = true;

      } else if (mode === 'hierarchy') {
        var levelGap = parseFloat((container.querySelector('.vg-hr-gap')||{}).value || 130);
        var nodeGap = parseFloat((container.querySelector('.vg-hr-node')||{}).value || 60);
        var rootType = (container.querySelector('.vg-hr-root')||{}).value || 'session';
        // BFS tree layout
        var roots = vis.filter(function(n){return n.type===rootType || n.type===rootType.charAt(0).toUpperCase()+rootType.slice(1);});
        if(!roots.length) roots = [vis[0]]; // fallback
        var adjOut = {};
        state.edges.forEach(function(e){ if(!adjOut[e.from])adjOut[e.from]=[]; adjOut[e.from].push(e.to); });
        var visited = new Set();
        var xCursor = 0;
        function placeTree(id,depth){
          if(visited.has(id))return;
          visited.add(id);
          var n=state.nodeIndex[id]; if(!n||n._hidden)return;
          var children=(adjOut[id]||[]).filter(function(cid){return state.nodeIndex[cid]&&!state.nodeIndex[cid]._hidden&&!visited.has(cid);});
          if(!children.length){
            if(n){n.x=xCursor;n.y=depth*levelGap;n.vx=0;n.vy=0;}
            xCursor+=nodeGap;
          } else {
            var startX=xCursor;
            children.forEach(function(cid){placeTree(cid,depth+1);});
            if(n){n.x=(startX+xCursor)/2;n.y=depth*levelGap;n.vx=0;n.vy=0;}
          }
        }
        roots.forEach(function(r){placeTree(r.id,0);});
        // Place any unvisited nodes
        vis.forEach(function(n){if(!visited.has(n.id)){n.x=xCursor;n.y=0;n.vx=0;n.vy=0;xCursor+=nodeGap;}});
        state.frozen = true;

      } else if (mode === 'radial') {
        var radius = parseFloat((container.querySelector('.vg-rd-radius')||{}).value || 200);
        var centre = state.selected || vis[0];
        if(!centre)return;
        var adjAll={};
        state.edges.forEach(function(e){if(!adjAll[e.from])adjAll[e.from]=[];if(!adjAll[e.to])adjAll[e.to]=[];adjAll[e.from].push(e.to);adjAll[e.to].push(e.from);});
        var visited2=new Set([centre.id]);
        var queue=[{id:centre.id,level:0,angle:0,span:Math.PI*2}];
        centre.x=0;centre.y=0;centre.vx=0;centre.vy=0;
        while(queue.length){
          var q=queue.shift();
          var nbrs=(adjAll[q.id]||[]).filter(function(nid){return !visited2.has(nid)&&state.nodeIndex[nid]&&!state.nodeIndex[nid]._hidden;});
          if(!nbrs.length)continue;
          var r2=radius*(q.level+1),step=q.span/nbrs.length;
          nbrs.forEach(function(nid,i){
            visited2.add(nid);
            var a=q.angle-q.span/2+step*(i+0.5);
            var nn=state.nodeIndex[nid];
            if(nn){nn.x=Math.cos(a)*r2;nn.y=Math.sin(a)*r2;nn.vx=0;nn.vy=0;}
            queue.push({id:nid,level:q.level+1,angle:a,span:step});
          });
        }
        state.frozen = true;
      }
      // Fit after layout
      if (fitBtn) fitBtn.onclick();
    }

    // ── Session loading ─────────────────────────────────────────────────
    var _loadSessBtn = container.querySelector('[data-load-sess]');
    if (_loadSessBtn) _loadSessBtn.addEventListener('click', _loadSessionList);

    async function _loadSessionList(){
      if (!_spListEl) return;
      _spListEl.innerHTML = '<span style="font-size:9px;color:var(--dim,#6a6058)">Loading...</span>';
      try {
        var [sr,mr] = await Promise.all([
          fetch(apiBase+'/memory/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:'',limit:60,record_type:'event'})}),
          fetch(apiBase+'/memory/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:'',limit:80,record_type:'message',category:'chat'})}),
        ]);
        var sd=await sr.json(), md=await mr.json();
        var map={};
        (sd.results||[]).forEach(function(r){var rec=r.record||r;var sid=rec.session_id;if(!sid)return;if(!map[sid])map[sid]={sid:sid,ts:rec.created_at||'',agent:'',preview:'',count:0,name:''};var tags=Array.isArray(rec.tags)?rec.tags:[];if(tags.indexOf('name')>=0&&tags.indexOf('session')>=0&&rec.text&&!map[sid].name)map[sid].name=rec.text.trim().slice(0,60);});
        (md.results||[]).forEach(function(r){var rec=r.record||r;var sid=rec.session_id;if(!sid)return;if(!map[sid])map[sid]={sid:sid,ts:rec.created_at||'',agent:'',preview:'',count:0,name:''};map[sid].count++;if(!map[sid].preview&&rec.text)map[sid].preview=rec.text.slice(0,50);});
        _sessions=Object.values(map).filter(function(s){return s.sid;}).sort(function(a,b){return b.ts.localeCompare(a.ts);});
        _renderSessions(_sessions);
        var spSearch=container.querySelector('.vg-sp-search');
        if(spSearch)spSearch.style.display=_sessions.length>3?'':'none';
      }catch(e){_spListEl.innerHTML='<span style="color:var(--err,#c96b6b);font-size:9px">'+esc(e.message)+'</span>';}
    }
    function _renderSessions(items){
      if(!_spListEl)return;
      var curSid='';try{curSid=window.parent._veraSessionId||'';}catch(e){}
      if(!items.length){_spListEl.innerHTML='<span style="font-size:9px;color:var(--dim,#6a6058)">No sessions</span>';return;}
      _spListEl.innerHTML=items.map(function(s){
        var on=_selectedSids.has(s.sid);
        return '<div class="vg-sp'+(on?' on':'')+'" data-sid="'+esc(s.sid)+'" title="'+esc(s.sid)+'">'+(s.sid===curSid?'\u25b6 ':'')+esc(s.name||s.sid.slice(-12))+'<div style="font-size:7.5px;color:var(--dim,#6a6058)">'+s.count+' msgs \xb7 '+(s.ts||'').slice(0,10)+'</div></div>';
      }).join('');
    }
    if(_spListEl) _spListEl.addEventListener('click',function(ev){
      var el=ev.target.closest('.vg-sp');if(!el)return;
      var sid=el.dataset.sid;
      if(_selectedSids.has(sid))_selectedSids.delete(sid);else _selectedSids.add(sid);
      el.classList.toggle('on',_selectedSids.has(sid));
      if(_selectedSids.size>0) _loadSelectedSessions();
      else _loadMemory(_memSelEl?_memSelEl.value:'session');
    });
    async function _loadSelectedSessions(){
      var allN=[],allE=[];
      for(var sid of _selectedSids){
        try{var r=await fetch(apiBase+'/memory/graph/full?mode=session&session_id='+encodeURIComponent(sid)+'&limit_nodes=300&limit_edges=1500');var d=await r.json();if(d.nodes)allN=allN.concat(d.nodes);if(d.edges)allE=allE.concat(d.edges);}catch(e){}
      }
      var vn=allN.map(function(n){return{id:n.id,label:(n.summary||n.text||n.capability||n.id||'').slice(0,50),type:n.record_type||'Memory',props:n,r:8+parseFloat(n.importance||0.5)*8};});
      var ve=allE.map(function(e){return{from:e.from_id||e.from,to:e.to_id||e.to,rel:e.relation||'RELATED'};});
      load({nodes:vn,edges:ve});
    }
    var spSearchEl=container.querySelector('.vg-sp-search');
    if(spSearchEl)spSearchEl.addEventListener('input',function(){
      var q=spSearchEl.value.toLowerCase().trim();
      _renderSessions(q?_sessions.filter(function(s){return(s.name||s.sid).toLowerCase().indexOf(q)>=0||(s.preview||'').toLowerCase().indexOf(q)>=0;}):_sessions);
    });

    // ── Keyboard navigation ─────────────────────────────────────────────
    canvas.setAttribute('tabindex','0');
    canvas.addEventListener('keydown', function(ev){
      var PAN=40;
      if(ev.key==='ArrowLeft'){state.off.x+=PAN;ev.preventDefault();}
      else if(ev.key==='ArrowRight'){state.off.x-=PAN;ev.preventDefault();}
      else if(ev.key==='ArrowUp'){state.off.y+=PAN;ev.preventDefault();}
      else if(ev.key==='ArrowDown'){state.off.y-=PAN;ev.preventDefault();}
      else if(ev.key==='Tab'){
        ev.preventDefault();
        var vis2=state.nodes.filter(function(n){return !n._hidden;});
        if(!vis2.length)return;
        var curIdx=-1;
        if(state.selected){curIdx=vis2.indexOf(state.selected);}
        var next=ev.shiftKey?(curIdx<=0?vis2.length-1:curIdx-1):(curIdx>=vis2.length-1?0:curIdx+1);
        var nn=vis2[next];
        state.selected=nn;
        state.off.x=W/2-nn.x*state.scale;
        state.off.y=H/2-nn.y*state.scale;
        showDetail(nn);
      }
    });


    // ════════════════════════════════════════════════════════════════════
    // DETAIL DRAWER + CONTEXT-AWARE ACTION MENU
    // ════════════════════════════════════════════════════════════════════
    var _excludeSections = new Set(opts.excludeSections || []);
    // ── Full node-details modal (centred, like the table viewer) ────────────
    // Shows ALL properties untruncated, with clickable URLs. Long text bodies
    // get their own scrollable block. If opts.onNodeDetail(node) is provided it
    // is used to fetch the authoritative full record; otherwise we render from
    // the node's own props. opts.fullDetailUrl(node) -> a URL string to fetch.
    var _fullModalEl = null;
    function _escAttr(s){ return esc(String(s == null ? '' : s)); }
    function _isUrl(v){ return typeof v === 'string' && /^https?:\/\/\S+$/i.test(v.trim()); }
    function _renderDetailValue(v){
      if (v == null) return '<span style="color:var(--dim,#6a6058)">\u2014</span>';
      if (typeof v === 'object') {
        var json = JSON.stringify(v, null, 2);
        return '<pre style="margin:0;white-space:pre-wrap;word-break:break-word;font-family:var(--mono,monospace);font-size:10px;color:var(--text,#ddd5c8)">' + esc(json) + '</pre>';
      }
      var s = String(v);
      if (_isUrl(s)) {
        return '<a href="' + _escAttr(s) + '" target="_blank" rel="noopener" style="color:var(--acc,#5a9e8f);word-break:break-all">' + esc(s) + '</a>';
      }
      // linkify any URLs embedded in longer text
      if (/https?:\/\//i.test(s)) {
        var html = esc(s).replace(/(https?:\/\/[^\s<]+)/gi, function(m){
          return '<a href="' + _escAttr(m) + '" target="_blank" rel="noopener" style="color:var(--acc,#5a9e8f)">' + m + '</a>';
        });
        return '<span style="white-space:pre-wrap;word-break:break-word">' + html + '</span>';
      }
      return '<span style="white-space:pre-wrap;word-break:break-word">' + esc(s) + '</span>';
    }
    function _buildFullDetailHTML(node, record){
      var props = record || node.props || {};
      var rows = [];
      rows.push(['id', node.id]);
      if (node.type) rows.push(['type', node.type]);
      Object.keys(props).forEach(function(k){
        if (k === 'id' || (k.charAt(0) === '_')) return;   // skip internal keys
        rows.push([k, props[k]]);
      });
      return rows.map(function(kv){
        var k = kv[0], v = kv[1];
        var longText = (typeof v === 'string' && v.length > 200);
        return '<div style="display:flex;gap:10px;padding:6px 0;border-bottom:1px solid var(--border,#3a3530);align-items:flex-start">' +
          '<div style="min-width:120px;max-width:120px;color:var(--dim2,#8a7e70);font-size:9px;text-transform:uppercase;letter-spacing:.4px;font-family:var(--mono,monospace);flex-shrink:0">' + esc(k) + '</div>' +
          '<div style="flex:1;min-width:0;font-size:11px;color:var(--text,#ddd5c8)' + (longText ? ';max-height:300px;overflow-y:auto' : '') + '">' +
            _renderDetailValue(v) + '</div>' +
        '</div>';
      }).join('');
    }
    function _openFullModal(title, bodyHTML){
      if (!_fullModalEl) {
        _fullModalEl = document.createElement('div');
        _fullModalEl.className = 'vg-full-modal';
        _fullModalEl.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;' +
          'display:flex;align-items:center;justify-content:center';
        _fullModalEl.onclick = function(ev){ if (ev.target === _fullModalEl) _fullModalEl.style.display = 'none'; };
        (document.body || document.documentElement).appendChild(_fullModalEl);
      }
      _fullModalEl.style.display = 'flex';
      _fullModalEl.innerHTML =
        '<div style="width:min(680px,90vw);max-height:82vh;display:flex;flex-direction:column;' +
          'background:var(--bg1,#1f1d1a);border:1px solid var(--border,#3a3530);border-radius:6px;' +
          'box-shadow:0 8px 40px rgba(0,0,0,.6);overflow:hidden">' +
          '<div style="display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--border,#3a3530);background:var(--bg2,#272421)">' +
            '<div style="flex:1;min-width:0;font-size:12px;font-weight:600;color:var(--text,#ddd5c8);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(title) + '</div>' +
            '<button class="vg-fm-close" style="background:none;border:none;color:var(--dim,#6a6058);font-size:18px;cursor:pointer;line-height:1;padding:0 4px">\u00d7</button>' +
          '</div>' +
          '<div class="vg-fm-body" style="padding:10px 14px;overflow-y:auto;flex:1">' + bodyHTML + '</div>' +
        '</div>';
      _fullModalEl.querySelector('.vg-fm-close').onclick = function(){ _fullModalEl.style.display = 'none'; };
    }
    async function _showFullDetails(node){
      var title = node.label || (node.props && (node.props.title || node.props.name)) || node.id;
      _openFullModal(title, '<div style="color:var(--dim,#6a6058);font-size:10px">Loading\u2026</div>');
      var record = null;
      try {
        if (typeof opts.onNodeDetail === 'function') {
          record = await opts.onNodeDetail(node, instance);
        } else if (typeof opts.fullDetailUrl === 'function') {
          var url = opts.fullDetailUrl(node);
          if (url) { var res = await fetch(url); var j = await res.json(); record = j.record || j.node || j.data || j; }
        }
      } catch(e){ /* fall back to local props */ }
      _openFullModal(title, _buildFullDetailHTML(node, record));
    }

    async function showDetail(node){
      if (!detailEl) return;
      state.selected = node;
      detailEl.style.display = 'block';

      var props = node.props || {};
      // Build draggable field tokens
      var tokensHTML = '';
      function _ft(k,v){
        var d=String(v==null?'':v); var ds=d.length>120?d.slice(0,120)+'\u2026':d;
        return '<div class="vg-ft" draggable="true" data-val="'+esc(d)+'" ondragstart="event.dataTransfer.setData(\'text/plain\',this.dataset.val)"><span class="vg-ft-k">'+esc(k)+'</span><span class="vg-ft-v">'+esc(ds)+'</span></div>';
      }
      if(node.id) tokensHTML += _ft('id', node.id);
      if(node.type) tokensHTML += _ft('type', node.type);
      Object.keys(props).forEach(function(k){
        var v=props[k]; if(v===null||v===undefined||v===''||k==='id')return;
        tokensHTML += _ft(k, typeof v==='object'?JSON.stringify(v).slice(0,200):String(v).slice(0,200));
      });
      // Build edge list
      var edgesForNode = state.edges.filter(function(e){return e.from===node.id||e.to===node.id;}).slice(0,15);
      var edgeListHTML = '';
      if(edgesForNode.length){
        edgeListHTML = '<div style="font-size:7.5px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px;margin:8px 0 3px">Connections</div>'+
          edgesForNode.map(function(e){
            var other=e.from===node.id?e.to:e.from;
            var oN=state.nodeIndex[other];
            return '<div style="padding:1px 0;display:flex;align-items:center;gap:4px;font-size:9px"><span style="color:var(--dim,#6a6058)">'+(e.from===node.id?'\u2192':'\u2190')+'</span><span style="color:'+edgeColor(e.rel)+'">'+esc(e.rel||'?')+'</span><span style="color:var(--dim2,#8a7e70);cursor:pointer" data-focus-id="'+esc(other)+'">'+esc(((oN&&oN.label)||other).slice(0,28))+'</span></div>';
          }).join('');
      }
      var propsHTML = tokensHTML;

      var nodeLabel = node.type ||
                      (node.labels && node.labels[0]) ||
                      (node.label && /^[A-Z]/.test(node.label) ? node.label : 'Node');
      var displayName = node.label || props.title || props.name || props.url || node.id;

      detailEl.innerHTML =
        '<div style="display:flex;align-items:center;gap:5px;padding:8px;border-bottom:1px solid var(--border);background:var(--bg2);position:sticky;top:0;z-index:1">' +
          '<div style="flex:1;min-width:0">' +
            '<div style="font-size:11px;font-weight:600;color:' + nodeColor(node) + ';overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
              esc(displayName) + '</div>' +
            '<div style="font-size:9px;color:var(--dim2);font-family:var(--mono,monospace)">' + esc(nodeLabel) +
              (node.type === 'Entity' && node.props && node.props.type ? ' · ' + esc(node.props.type) : '') +
            '</div>' +
          '</div>' +
          '<button class="vg-detail-full" title="View full, untruncated details" style="background:none;border:1px solid var(--border,#3a3530);color:var(--dim2,#8a7e70);font-size:8px;cursor:pointer;padding:2px 6px;border-radius:3px;white-space:nowrap">Full details</button>' +
          '<button class="vg-detail-close" style="background:none;border:none;color:var(--dim);font-size:14px;cursor:pointer;padding:0 4px">×</button>' +
        '</div>' +
        '<div style="padding:8px">' +
          '<div style="font-size:8.5px;color:var(--dim2);font-family:var(--mono,monospace);margin-bottom:6px;word-break:break-all">' + esc(node.id) + '</div>' +
          ((props.url || props.link) && _isUrl(props.url || props.link) ?
            '<div style="margin-bottom:6px"><a href="' + _escAttr(props.url || props.link) + '" target="_blank" rel="noopener" style="font-size:10px;color:var(--acc,#5a9e8f);word-break:break-all">' + esc(props.url || props.link) + ' \u2197</a></div>' : '') +
          '<div class="vg-builtin-acts" style="margin-bottom:8px"></div>' +
          (_excludeSections.has('actions') ? '' :
          '<div style="font-size:8.5px;color:var(--dim2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Actions for ' + esc(nodeLabel) + '</div>' +
          '<div class="vg-actions" style="display:flex;flex-direction:column;gap:4px;margin-bottom:10px"><span style="color:var(--dim);font-size:9.5px">Loading actions…</span></div>') +
          '<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border)"><div style="font-size:8.5px;color:var(--dim2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Properties</div>' + propsHTML + '</div>' +
          '<div style="margin-top:6px">' + edgeListHTML + '</div>' +
          // Context & expansion
          '<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border,#3a3530)">' +
            '<div style="display:flex;align-items:center;gap:4px;margin-bottom:4px;flex-wrap:wrap"><span style="font-size:8px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px">Expand</span>' +
            '<button class="vg-ctx-btn" style="font-size:8px;padding:1px 6px;background:rgba(90,158,143,.1);border:1px solid var(--acc,#5a9e8f);color:var(--acc,#5a9e8f);border-radius:3px;cursor:pointer" title="Use the context system to find semantically related records, skills, ontologies">Context</button>' +
            '<button class="vg-traverse-btn" style="font-size:8px;padding:1px 6px;background:rgba(201,149,90,.1);border:1px solid var(--acc3,#c9955a);color:var(--acc3,#c9955a);border-radius:3px;cursor:pointer" title="Walk the graph from this node">Traverse</button>' +
            '<button class="vg-expand-btn" style="font-size:8px;padding:1px 6px;background:rgba(107,155,210,.1);border:1px solid var(--acc4,#6b9bd2);color:var(--acc4,#6b9bd2);border-radius:3px;cursor:pointer" title="Expand edges into visible nodes">Edges</button></div>' +
            '<div class="vg-ctx-result" style="display:none;font-size:9px;color:var(--dim2,#8a7e70);max-height:180px;overflow-y:auto;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);border-radius:3px;padding:5px 7px;white-space:pre-wrap;font-family:var(--mono,monospace)"></div>' +
          '</div>' +
          // Cap runner
          (_excludeSections.has('capRunner') ? '' :
          '<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border,#3a3530)">' +
            '<div style="font-size:8px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Run Capability</div>' +
            '<select class="vg-cap-sel"><option value="">\u2014 pick capability \u2014</option></select>' +
            '<div class="vg-cap-params" style="display:flex;flex-direction:column;gap:3px;margin:4px 0"></div>' +
            '<button class="vg-cap-run">\u25b6 Run</button>' +
            '<div class="vg-cap-result" style="display:none;margin-top:4px;font-size:9px;font-family:var(--mono,monospace);background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);border-radius:3px;padding:4px 6px;max-height:100px;overflow-y:auto;white-space:pre-wrap"></div>' +
          '</div>') +
          '<div class="vg-detail-extra" style="margin-top:8px">' +
            _builtSections +
          '</div>' +
        '</div>';

      detailEl.querySelector('.vg-detail-close').onclick = function(){
        detailEl.style.display = 'none';
        state.selected = null;
      };
      var fullBtn = detailEl.querySelector('.vg-detail-full');
      if (fullBtn) fullBtn.onclick = function(){ _showFullDetails(node); };
      // Wire edge focus clicks
      detailEl.querySelectorAll('[data-focus-id]').forEach(function(el){
        el.onclick = function(){ instance.focusNode(el.dataset.focusId); };
      });
      // Wire built-in section handlers
      _wireSections(detailEl, node);
      // Wire context / traverse / edges expand
      var ctxBtn = detailEl.querySelector('.vg-ctx-btn');
      var traverseBtn = detailEl.querySelector('.vg-traverse-btn');
      var expandBtn = detailEl.querySelector('.vg-expand-btn');
      var ctxResult = detailEl.querySelector('.vg-ctx-result');
      if(ctxBtn) ctxBtn.onclick = async function(){
        if(ctxResult){ctxResult.style.display='block';ctxResult.textContent='Assembling context...';}
        try{
          var query = node.label || node.id;
          var sid = (node.props && node.props.session_id) || '';
          try{if(!sid)sid=window.parent._veraSessionId||'';}catch(_){}
          // Use the context system — assembles skills, ontologies, caps, memory
          var res = await fetch(apiBase+'/context/assemble',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
            message:query, attach_memory:true, attach_caps:'auto', attach_ontologies:'auto',
            session_id:sid, memory_limit:5, agent_name:''
          })});
          var data = await res.json();
          if(ctxResult){
            ctxResult.textContent = data.preview || data.system_prompt || JSON.stringify(data,null,2).slice(0,800);
          }
          // Also fetch and add memory nodes as connected context — graphed as
          // nodes linked to the source with DOTTED edges so context results are
          // visually distinct from the structural graph.
          try{
            var memRes = await fetch(apiBase+'/memory/agent/context',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
              session_id:sid, query:query, limit:8
            })});
            var memData = await memRes.json();
            var ctxAdded = 0;
            // Add any structured context items as dotted-linked nodes
            var items = memData.results || memData.records || memData.memories || memData.items || [];
            (Array.isArray(items) ? items : []).forEach(function(r){
              var n2 = r.node || r.record || r;
              if (!n2 || !n2.id) return;
              var a = addNode({
                id: n2.id,
                label: String(n2.summary || n2.text || n2.title || n2.id).slice(0, 50),
                type: n2.record_type || n2.type || 'Context',
                props: n2,
                layer: 'informational',
                r: 7
              });
              if (a) {
                a._pulseUntil = Date.now() + 2000;
                addEdge({ from: node.id, to: n2.id, rel: r.relation || 'CONTEXT', _dashed: true,
                          layer: 'informational' });
                ctxAdded++;
              }
            });
            if (ctxAdded) { _computeClusters(); wake(); }
            if(memData.context && typeof memData.context === 'string'){
              ctxResult.textContent += '\n\n--- Agent Memory Context ---\n' + memData.context.slice(0,400) +
                (ctxAdded ? ('\n\n(' + ctxAdded + ' context nodes added with dotted links)') : '');
            } else if (ctxAdded) {
              ctxResult.textContent += '\n\n(' + ctxAdded + ' context nodes added with dotted links)';
            }
          }catch(_){}
        }catch(e){if(ctxResult)ctxResult.textContent='Error: '+e.message;}
      };
      if(traverseBtn) traverseBtn.onclick = async function(){
        if(ctxResult){ctxResult.style.display='block';ctxResult.textContent='Traversing...';}
        try{
          var res = await fetch(apiBase+'/memory/traverse',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({start_id:node.id,depth:2,limit:20})});
          var data = await res.json();
          var results = data.results || [];
          if(ctxResult) ctxResult.textContent = results.length+' connected nodes found';
          results.forEach(function(r){
            var n2=r.node||r.record||r;if(!n2||!n2.id)return;
            var added=addNode({id:n2.id,label:(n2.summary||n2.text||n2.id).slice(0,50),type:n2.record_type||n2.type||'Memory',props:n2,r:7});
            if(added){added._pulseUntil=Date.now()+1500;addEdge({from:node.id,to:n2.id,rel:r.relation||'RELATED'});}
          });
          wake();
        }catch(e){if(ctxResult)ctxResult.textContent='Error: '+e.message;}
      };
      if(expandBtn) expandBtn.onclick = async function(){
        // Expand all edges from this node into visible nodes
        if(ctxResult){ctxResult.style.display='block';ctxResult.textContent='Expanding edges...';}
        try{
          // Fabric entity expansion
          var res1 = await fetch(apiBase+'/fabric/entity_graph/snapshot?include_datasets=1&include_records=1&dataset_id='+encodeURIComponent(node.id));
          var d1 = await res1.json();
          var added1=0;
          (d1.nodes||[]).forEach(function(en){ var a=addNode({id:en.id,label:en.label||en.id,type:en.type||'Entity',props:en.props||{},r:en.r||8}); if(a){a._pulseUntil=Date.now()+1500;added1++;}});
          (d1.edges||[]).forEach(function(ee){ addEdge({from:ee.from,to:ee.to,rel:ee.rel||'RELATED'}); });
          // Also try graph traversal for memory nodes
          try{
            var res2 = await fetch(apiBase+'/memory/traverse',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({start_id:node.id,depth:1,limit:30})});
            var d2 = await res2.json();
            (d2.results||[]).forEach(function(r){
              var n2=r.node||r.record||r;if(!n2||!n2.id)return;
              var a2=addNode({id:n2.id,label:(n2.summary||n2.text||n2.id).slice(0,50),type:n2.record_type||n2.type||'Memory',props:n2,r:7});
              if(a2){a2._pulseUntil=Date.now()+1500;added1++;addEdge({from:node.id,to:n2.id,rel:r.relation||'RELATED'});}
            });
          }catch(_){}
          if(ctxResult) ctxResult.textContent = added1+' nodes expanded';
          wake();
        }catch(e){if(ctxResult)ctxResult.textContent='Error: '+e.message;}
      };
      // Wire cap runner
      _wireCapRunner(detailEl, node);

      // Built-in expand/records
      var builtin = detailEl.querySelector('.vg-builtin-acts');
      var hasExpanded = state.expanded[node.id] && state.expanded[node.id].length;
      builtin.innerHTML =
        '<button class="vg-act vg-act-' + (hasExpanded ? 'collapse' : 'expand') + '" style="margin:2px 3px 2px 0;padding:3px 8px;font-size:9.5px;background:' + (hasExpanded ? 'var(--err,#c96b6b)' : 'var(--acc2,#8fb87a)') + ';color:#111;border:none;border-radius:3px;cursor:pointer">' +
          (hasExpanded ? 'Collapse (' + state.expanded[node.id].length + ')' : 'Expand') + '</button>' +
        '<button class="vg-act vg-act-records" style="margin:2px 3px;padding:3px 8px;font-size:9.5px;background:var(--bg2);color:var(--text);border:1px solid var(--border);border-radius:3px;cursor:pointer">Show Records</button>';
      var btnExp = builtin.querySelector('.vg-act-expand');
      var btnCol = builtin.querySelector('.vg-act-collapse');
      var btnRec = builtin.querySelector('.vg-act-records');
      if (btnExp) btnExp.onclick = function(){ expandEntities(node); };
      if (btnCol) btnCol.onclick = function(){ collapseNode(node); };
      if (btnRec) btnRec.onclick = function(){ expandRecords(node); };

      // Server registry-driven actions
      if (state.actionsEnabled) {
        var actsEl = detailEl.querySelector('.vg-actions');
        var actions = state.actionsCache[nodeLabel];
        if (!actions) {
          actions = await fetchActions(apiBase, nodeLabel, node.id);
          state.actionsCache[nodeLabel] = actions;
        }
        if (!actions || !actions.length) {
          actsEl.innerHTML = '<span style="color:var(--dim);font-size:9.5px">No actions registered for ' + esc(nodeLabel) + '.</span>';
        } else {
          actsEl.innerHTML = actions.map(function(a, idx){
            return _renderActionCard(a, idx, node);
          }).join('');
          // Wire up Run buttons
          actions.forEach(function(a, idx){
            var btn = actsEl.querySelector('[data-act-run="' + idx + '"]');
            if (btn) btn.onclick = function(){ runAction(a, idx, node); };
          });
        }
      } else {
        var actsEl = detailEl.querySelector('.vg-actions');
        if (actsEl) actsEl.style.display = 'none';
      }

      if (opts.onSelect) opts.onSelect(node, instance);
    }

    function _renderActionCard(a, idx, node){
      var dangerStyle = a.danger ? 'border-color:var(--err)' : '';
      var optHtml = (a.options || []).map(function(o){
        return _renderOption(idx, o);
      }).join('');
      return (
        '<div class="vg-action" data-act-idx="' + idx + '" style="background:var(--bg2);border:1px solid var(--border);border-radius:3px;padding:6px 8px;' + dangerStyle + '">' +
          '<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">' +
            '<span style="font-family:var(--mono);color:' + (a.danger ? 'var(--err)' : 'var(--acc)') + ';width:14px;text-align:center">' + esc(a.icon || '•') + '</span>' +
            '<span style="font-size:10.5px;font-weight:600;flex:1">' + esc(a.label) + '</span>' +
            '<button data-act-run="' + idx + '" class="vg-act" style="padding:2px 8px;font-size:9.5px;background:' + (a.danger ? 'var(--err)' : 'var(--acc)') + ';color:#111;border:none;border-radius:3px;cursor:pointer">Run</button>' +
          '</div>' +
          (a.context ? '<div style="font-size:9px;color:var(--dim2);line-height:1.4;margin-bottom:4px">' + esc(a.context) + '</div>' : '') +
          (optHtml ? '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:4px">' + optHtml + '</div>' : '') +
          '<div class="vg-stream" id="vg-stream-' + idx + '" style="display:none;margin-top:6px;padding:5px 7px;background:var(--bg0);border:1px solid var(--border);border-radius:2px;font-family:var(--mono,monospace);font-size:9px;color:var(--dim);max-height:140px;overflow-y:auto;line-height:1.5"></div>' +
        '</div>'
      );
    }

    function _renderOption(actIdx, o){
      var lbl = esc(o.label || o.name);
      var id  = 'vg-opt-' + actIdx + '-' + o.name;
      if (o.type === 'bool') {
        return '<label style="font-size:9px;color:var(--dim2);display:flex;align-items:center;gap:3px"><input type="checkbox" id="' + id + '"' + (o.default ? ' checked' : '') + '> ' + lbl + '</label>';
      }
      if (o.type === 'select' && Array.isArray(o.options)) {
        return '<label style="font-size:9px;color:var(--dim2);display:flex;align-items:center;gap:3px">' + lbl + ': <select id="' + id + '" style="font-size:9.5px;padding:1px 3px">' +
          o.options.map(function(v){
            return '<option value="' + esc(v) + '"' + (v === o.default ? ' selected' : '') + '>' + esc(v) + '</option>';
          }).join('') + '</select></label>';
      }
      if (o.type === 'int' || o.type === 'float') {
        return '<label style="font-size:9px;color:var(--dim2);display:flex;align-items:center;gap:3px">' + lbl + ': <input type="number" id="' + id + '" value="' + esc(o.default !== undefined ? String(o.default) : '') + '" style="width:60px;font-size:9.5px;padding:1px 3px"></label>';
      }
      return '<label style="font-size:9px;color:var(--dim2);display:flex;align-items:center;gap:3px">' + lbl + ': <input type="text" id="' + id + '" value="' + esc(o.default !== undefined ? String(o.default) : '') + '" style="width:120px;font-size:9.5px;padding:1px 3px"></label>';
    }

    function _collectOptions(actIdx, defs){
      var out = {};
      (defs || []).forEach(function(o){
        var el = document.getElementById('vg-opt-' + actIdx + '-' + o.name);
        if (!el) return;
        var v;
        if (o.type === 'bool')        v = el.checked;
        else if (o.type === 'int')    v = parseInt(el.value, 10);
        else if (o.type === 'float')  v = parseFloat(el.value);
        else                           v = el.value;
        out[o.name] = v;
      });
      return out;
    }

    // ── Memory layer helpers ────────────────────────────────────────────────
    // Each capability run via the RHM is captured as a Memory node (cap + target
    // + status) linked to the node it ran against, in the 'memory' layer. This
    // is INDEPENDENT of the cap-tracking panel — as long as the memory layer is
    // enabled, the run is recorded both as a graph node AND written to the
    // memory backend (/memory/store). The layer is toggleable from the LHM.
    //   • opts.memoryLayer === false  → disable entirely
    //   • the layer starts ENABLED but hidden? No — enabled and visible so the
    //     user sees memories accrue. (Set its visibility off in the LHM to hide.)
    //   • opts.memoryStore === false  → keep the graph node but don't POST to
    //     the memory backend (graph-only memory layer).
    var _memSeq = 0;
    function _memoryEnabled(){
      if (opts.memoryLayer === false) return false;
      var lyr = state.layers && state.layers['memory'];
      // enabled unless the user explicitly turned the layer's visibility off
      return !lyr || lyr.visible !== false;
    }
    function _recordActivity(action, node, status){
      if (!_memoryEnabled()) {
        if (typeof console !== 'undefined') console.log('[vera-graph] memory layer disabled, skipping record');
        return null;
      }
      _ensureLayer('memory');
      var cap = action.capability || action.id || 'cap';
      var aid = 'memory.' + cap + '.' + (Date.now()) + '.' + (_memSeq++);
      // Position the memory node NEAR the target so the user can see it linked
      // (spawning at the periphery makes it invisible in a large graph)
      var spawnX = (node && node.x) ? node.x + (Math.random() - 0.5) * 80 : undefined;
      var spawnY = (node && node.y) ? node.y + (Math.random() - 0.5) * 80 : undefined;
      var added = addNode({
        id: aid,
        label: '⚙ ' + (cap.split('.').pop() || cap),
        type: 'Memory',
        layer: 'memory',
        x: spawnX, y: spawnY,
        props: {
          capability: cap,
          target: node && node.id,
          target_label: node && (node.label || node.id),
          status: status || 'running',
          started_at: new Date().toISOString(),
          source: 'vera-graph-rhm',
        },
        r: 8,
      });
      if (typeof console !== 'undefined') console.log('[vera-graph] memory node', added ? 'created' : 'FAILED', aid, 'for', cap, 'on', node && node.id);
      if (added) {
        added._pulseUntil = Date.now() + 1800;
        if (node && node.id) {
          addEdge({ from: node.id, to: aid, rel: 'REMEMBERS', layer: 'memory', _dashed: true,
                    props: { capability: cap } });
        }
        _renderLayerUI();
        _applyVis(); draw();
      }
      // Write to the actual memory backend, regardless of cap-tracking settings.
      if (opts.memoryStore !== false && apiBase !== undefined) {
        var text = 'Ran capability ' + cap + ' on ' + (node && (node.label || node.id) || 'node');
        try {
          fetch(apiBase + '/memory/store', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              text: text,
              record_type: 'event',
              category: 'graph_action',
              metadata: {
                capability: cap, target: node && node.id,
                graph_layer: state.currentLayer, source: 'vera-graph-rhm',
                memory_node_id: aid,
              },
            }),
          }).then(function(r){ return r.json(); }).then(function(j){
            if (j && (j.id || j.memory_id)) {
              var n = state.nodeIndex[aid];
              if (n) { n.props.memory_id = j.id || j.memory_id; }
            }
          }).catch(function(){});
        } catch(e){}
      }
      // host hook (also fires regardless of cap-tracking)
      if (opts.onActivity) {
        try { opts.onActivity({ id: aid, capability: cap, target: node && node.id, status: status, instance: instance }); } catch(e){}
      }
      return aid;
    }
    function _updateActivity(aid, status, result){
      if (!aid) return;
      var n = state.nodeIndex[aid];
      if (!n) return;
      n.props = n.props || {};
      n.props.status = status;
      n.props.ended_at = new Date().toISOString();
      if (result && result.result && typeof result.result === 'object') {
        var r = result.result;
        var cnt = (r.results || r.records || r.matches || r.entities || []).length;
        if (cnt) n.props.result_count = cnt;
      }
      n._pulseUntil = Date.now() + 1200;
      if (opts.onActivity) {
        try { opts.onActivity({ id: aid, status: status, instance: instance, result: result }); } catch(e){}
      }
      draw();
    }

    async function runAction(action, idx, node){
      // Host override hook — return false to suppress default
      if (opts.onAction) {
        try {
          var rv = opts.onAction(action.id, node, instance, action);
          if (rv === false) return;
        } catch(_){}
      }
      // Built-in client-only actions
      if (action.capability === '__local') {
        _handleLocalAction(action, node);
        return;
      }
      if (action.confirm && !confirm(action.confirm)) return;

      // ── Memory layer ──────────────────────────────────────────────────────
      // Capture this capability run as a Memory node linked to the target node,
      // in the 'memory' layer, AND write it to the memory backend — regardless
      // of the cap-tracking panel's settings, as long as the memory layer is
      // enabled. Works on ANY graph (network / memory / fabric).
      var _activityNodeId = _recordActivity(action, node, 'running');

      var streamBox = detailEl.querySelector('#vg-stream-' + idx);
      if (streamBox) {
        streamBox.style.display = 'block';
        streamBox.innerHTML = '<div style="color:var(--acc)">▸ starting…</div>';
      }

      var defs = (state.actionsCache[node.type] || []).find(function(a){return a.id === action.id;});
      var options = _collectOptions(idx, (defs && defs.options) || action.options || []);
      var streamName = action.stream || (defs && defs.stream) || '';

      // Subscribe to the action's progress stream
      var unsub = null;
      var bus = opts.eventBus || _getSharedBus();
      if (streamName && bus) {
        unsub = bus.subscribe(streamName, function(ev){
          _renderStreamEvent(streamBox, ev);
          _forwardEventToGraph(ev);
        });
      }

      var nodeLabel = node.type ||
                      (node.labels && node.labels[0]) ||
                      (node.label && /^[A-Z]/.test(node.label) ? node.label : 'Node');

      var t0 = Date.now();
      var result;
      try {
        var res = await fetch(apiBase + '/fabric/graph/run_node_action', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({
            node_label: nodeLabel,
            node_id:    node.id,
            action_id:  action.id,
            options:    options,
          }),
        });
        result = await res.json();
      } catch(e) {
        result = {error: String(e && e.message || e)};
      }

      var elapsed = ((Date.now() - t0)/1000).toFixed(1);
      if (streamBox) {
        var line = document.createElement('div');
        if (result && result.ok) {
          line.style.color = 'var(--ok)';
          line.textContent = '▸ done in ' + elapsed + 's';
          streamBox.appendChild(line);
          // Render actual result data inline
          _renderActionResult(streamBox, action, result);
          // Also graph the results as properly-labelled nodes (one per result),
          // linked to the source node — see _graphActionResults.
          try { _graphActionResults(action, node, result); } catch(e){ if (typeof console!=='undefined') console.warn('graph results', e); }
          try { _updateActivity(_activityNodeId, result && result.ok ? 'done' : 'error', result); } catch(e){}
        } else {
          line.style.color = 'var(--err)';
          line.textContent = '▸ failed: ' + (result && result.error || 'unknown');
          streamBox.appendChild(line);
        }
        streamBox.scrollTop = streamBox.scrollHeight;
      }
      if (unsub) try { unsub(); } catch(_){}

      // Re-fetch the snapshot so persisted side-effects appear — but ONLY if
      // the current view was itself loaded via fetchSnapshot. If the host panel
      // populated the graph some other way (direct load, a dataset-scoped view,
      // a loom view), refetching a generic snapshot here would replace what the
      // user is looking at with the oldest 200 nodes in the database. Many
      // actions (summarise, show mentions, etc.) don't change the graph at all,
      // so a blind refetch is wrong for them too.
      setTimeout(function(){
        if (state.loadedViaSnapshot &&
            state.currentLayer && state.currentLayer !== 'memory' &&
            instance.fetchSnapshot) {
          // Reload the exact same scope the user was viewing (currentParams
          // carries dataset_id / label_filter / limit from the original call).
          instance.fetchSnapshot(state.currentLayer, state.currentParams || {});
        }
      }, 600);

      if (opts.onActionDone) opts.onActionDone(action, node, result, instance);
      // Fire a window-level event so sidebar panels (which can't set opts.onActionDone)
      // can react to completed server actions without patching opts.
      try {
        window.dispatchEvent(new CustomEvent('vg:action:done', {
          detail: { action_id: action.id, action: action, node: node,
                    result: result, instance: instance }
        }));
      } catch(_) {}
    }

    function _handleLocalAction(action, node){
      // Browse / open-record fall back to the dispatch event so the host
      // panel can decide what "browse" means.
      window.dispatchEvent(new CustomEvent('vg:action', {
        detail: {action_id: action.id, node: node, instance: instance},
      }));
    }

    /**
     * Render the actual result payload from a completed action inline in
     * the stream box so the user sees the data (summary, matches, entities)
     * instead of just "done in Xs".
     */
    /**
     * Graph the results of a completed capability as properly-labelled nodes.
     * Instead of a single opaque "results" node, each result becomes its own
     * node, id'd as  cap.<capname>.result.<i>  (and labelled from the result's
     * own title/name/url where available), linked back to the node the action
     * was run against. Results are tagged into the 'informational' layer so they
     * can be toggled. If opts.onActionResults is provided, it's called so the
     * host can pipe the new records through entity extraction / loom.
     */
    function _graphActionResults(action, node, result){
      if (!result || !result.ok) return;
      var r = result.result || {};
      var capName = action.capability || action.id || 'cap';
      var srcId = (node && node.id) ||
                  (action.args && action.args.record_id === '$id' && state.selected && state.selected.id) ||
                  (state.selected && state.selected.id) || null;

      // Locate the result collection. Try common shapes; fall back to treating
      // the whole result object as a single result if it has useful content.
      var items = r.results || r.records || r.matches || r.items ||
                  r.entities || r.pages || r.rows || r.data || null;
      var single = false;
      if (!Array.isArray(items)) {
        // a single-object result (e.g. one page / one summary) — still graph it
        var keys = Object.keys(r).filter(function(k){
          return ['ok','trace_id','action_id','capability','count','total'].indexOf(k) === -1;
        });
        if (!keys.length) return;
        items = [r];
        single = true;
      }
      if (!items.length) return;

      var newRecordIds = [];
      var added = 0;
      items.slice(0, 300).forEach(function(item, i){
        if (item == null) return;
        // derive a readable label and a stable per-result id
        var title = (typeof item === 'object'
          ? (item.title || item.name || item.label || item.url || item.text || item.value ||
             item.id || JSON.stringify(item).slice(0, 60))
          : String(item));
        var rid = (typeof item === 'object' && (item.id || item.url)) ||
                  ('cap.' + capName + '.result.' + i);
        // node type: prefer the item's own type, else a generic result type
        var type = (typeof item === 'object' && (item.type || (item.labels && item.labels[0]))) ||
                   'CapResult';
        var props = (typeof item === 'object') ? item : { value: item };
        // mark provenance so these are identifiable / filterable
        props = Object.assign({}, props, {
          _capability: capName,
          _result_index: i,
          _source_node: srcId || undefined,
        });
        var nodeAdded = addNode({
          id: rid,
          label: String(title).slice(0, 50),
          type: type,
          props: props,
          // results are informational unless the item declares otherwise
          layer: item.layer || item.group || 'informational',
        });
        if (nodeAdded) {
          nodeAdded._pulseUntil = Date.now() + 2200;
          added++;
          if (srcId && srcId !== rid) {
            // edge labelled with the capability so the provenance is visible
            addEdge({
              from: srcId, to: rid,
              rel: ('HAS_' + String(capName).split('.').pop().toUpperCase()).slice(0, 20),
              props: { capability: capName },
              layer: 'structural',     // the provenance link is structural
            });
          }
          if (type === 'FabricRecord' || type === 'Record' || item.url || item.text) {
            newRecordIds.push(rid);
          }
        }
      });

      if (added) {
        _computeClusters();
        _renderLayerUI();
        wake();
      }

      // Hand the new records to the host so it can run entity extraction / loom
      // over them (the graph itself doesn't own those backend pipelines). The
      // host can call instance.fetchSnapshot / addNode with the resulting
      // entities, which will stream into the informational layer.
      if (opts.onActionResults) {
        try {
          opts.onActionResults({
            capability: capName, sourceId: srcId,
            recordIds: newRecordIds, count: added,
            items: items, single: single, instance: instance,
          });
        } catch(e){ if (typeof console !== 'undefined') console.warn('onActionResults', e); }
      }
    }

    function _renderActionResult(box, action, result){
      if (!box || !result || !result.ok) return;
      var r = result.result || {};
      var wrap = document.createElement('div');
      wrap.style.cssText = 'margin-top:4px;padding:6px 8px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);border-radius:3px;font-size:9.5px;line-height:1.5;max-height:280px;overflow-y:auto;word-break:break-word';

      var aid = action.id || '';

      // ── Summarise action: show the generated summary ──
      if (aid === 'summarise' || aid === 'summarize') {
        var summary = r.summary || r.text || r.content || '';
        if (summary) {
          var hd = document.createElement('div');
          hd.style.cssText = 'font-weight:600;color:var(--acc,#5a9e8f);margin-bottom:4px;font-size:10px';
          hd.textContent = 'LLM Summary';
          wrap.appendChild(hd);
          var body = document.createElement('div');
          body.style.cssText = 'color:var(--text,#ddd5c8);white-space:pre-wrap';
          body.textContent = summary;
          wrap.appendChild(body);
          box.appendChild(wrap);
          box.scrollTop = box.scrollHeight;
          return;
        }
      }

      // ── Find related / Loom: show matched records ──
      if (aid === 'find_related' || aid === 'loom_match' || aid === 'run_loom') {
        var matches = r.matches || r.results || r.records || [];
        if (Array.isArray(matches) && matches.length) {
          var hd = document.createElement('div');
          hd.style.cssText = 'font-weight:600;color:var(--acc2,#8fb87a);margin-bottom:4px;font-size:10px';
          hd.textContent = 'Related Records (' + matches.length + ')';
          wrap.appendChild(hd);
          matches.forEach(function(m, i){
            var row = document.createElement('div');
            row.style.cssText = 'padding:3px 0;border-bottom:1px solid var(--border,#3a3530)';
            var title = m.title || m.text || m.id || '';
            var score = typeof m.score === 'number' ? ' (' + m.score.toFixed(3) + ')' : '';
            var ds = m.dataset_id ? ' · ' + m.dataset_id : '';
            row.innerHTML = '<span style="color:var(--acc,#5a9e8f);font-family:var(--mono,monospace);font-size:8.5px">#' +
              (i + 1) + '</span> ' + esc(String(title).slice(0, 120)) +
              '<span style="color:var(--dim2,#8a7e70)">' + esc(score + ds) + '</span>';
            wrap.appendChild(row);
            // Also add matched nodes to the graph with a pulse
            if (m.id) {
              var added = addNode({
                id: m.id,
                label: String(title).slice(0, 50),
                type: m.type || 'FabricRecord',
                props: m,
              });
              if (added) {
                added._pulseUntil = Date.now() + 2000;
                addEdge({from: (action.args && action.args.record_id === '$id' ? (state.selected && state.selected.id) : m.id), to: m.id, rel: 'RELATED_TO'});
              }
            }
          });
          box.appendChild(wrap);
          wake();
          box.scrollTop = box.scrollHeight;
          return;
        }
      }

      // ── Entity extraction: show extracted entities ──
      if (aid === 'extract_entities' || aid === 'extract_entities_record') {
        var entities = r.entities || r.results || [];
        var count = r.entity_count || r.count || (Array.isArray(entities) ? entities.length : 0);
        if (count > 0 || (Array.isArray(entities) && entities.length)) {
          var hd = document.createElement('div');
          hd.style.cssText = 'font-weight:600;color:var(--acc3,#c9955a);margin-bottom:4px;font-size:10px';
          hd.textContent = 'Entities Extracted (' + count + ')';
          wrap.appendChild(hd);
          if (Array.isArray(entities)) {
            entities.slice(0, 50).forEach(function(ent){
              var pill = document.createElement('span');
              pill.style.cssText = 'display:inline-block;margin:2px 3px 2px 0;padding:1px 6px;border-radius:8px;font-size:8.5px;background:rgba(90,158,143,.1);color:var(--acc,#5a9e8f);border:1px solid rgba(90,158,143,.18)';
              pill.textContent = (ent.type || ent.entity_type || '') + ': ' + (ent.name || ent.text || ent.value || '');
              wrap.appendChild(pill);
            });
          }
          box.appendChild(wrap);
          box.scrollTop = box.scrollHeight;
          return;
        }
      }

      // ── Generic fallback: show the result as JSON if it has useful data ──
      var keys = Object.keys(r);
      // Filter out internal keys
      var useful = keys.filter(function(k){ return k !== 'ok' && k !== 'trace_id' && k !== 'action_id' && k !== 'capability'; });
      if (useful.length) {
        var preview = {};
        useful.forEach(function(k){ preview[k] = r[k]; });
        var text = JSON.stringify(preview, null, 2);
        if (text.length > 20) { // Skip trivial results like {}
          var hd = document.createElement('div');
          hd.style.cssText = 'font-weight:600;color:var(--dim2,#8a7e70);margin-bottom:4px;font-size:10px';
          hd.textContent = 'Result';
          wrap.appendChild(hd);
          var pre = document.createElement('pre');
          pre.style.cssText = 'margin:0;font-family:var(--mono,monospace);font-size:8.5px;white-space:pre-wrap;color:var(--text,#ddd5c8)';
          pre.textContent = text.slice(0, 3000);
          wrap.appendChild(pre);
          box.appendChild(wrap);
          box.scrollTop = box.scrollHeight;
        }
      }
    }

    function _renderStreamEvent(box, ev){
      if (!box) return;
      var stage = ev.stage || ev.phase || ev.type || '';
      var msg   = ev.message || ev.current || '';
      var pages = ev.pages !== undefined ? ' [' + ev.pages + ' pages]' : '';
      var ents  = ev.entities !== undefined ? ' [' + ev.entities + ' entities]' : '';
      var rels  = ev.relations !== undefined ? ' [' + ev.relations + ' rels]' : '';
      var line = document.createElement('div');
      line.style.color = ev.error ? 'var(--err)' :
                          (stage === 'done' ? 'var(--ok)' : 'var(--acc2)');
      line.textContent = '▸ ' + stage + (msg ? ': ' + msg : '') + pages + ents + rels;
      box.appendChild(line);
      while (box.children.length > 200) box.removeChild(box.firstChild);
      box.scrollTop = box.scrollHeight;
    }

    function _forwardEventToGraph(ev){
      // If the event names a new node, add it live to the graph and pulse it
      if (ev.entity_name && ev.entity_type) {
        var eid = ev.entity_type + ':' + ev.entity_name;
        var added = addNode({
          id: eid, label: ev.entity_name, type: 'Entity',
          props: {type: ev.entity_type, name: ev.entity_name},
        });
        if (added) {
          added._pulseUntil = Date.now() + 1500;
          if (ev.from_url || ev.parent_url || ev.record_id) {
            addEdge({from: eid,
                      to: ev.from_url || ev.parent_url || ev.record_id,
                      rel: ev.dataset_id ? 'MENTIONED_IN' : 'CO_OCCURS'});
          }
        }
      }
      if (ev.url && ev.dataset_id && ev.stage === 'page_added') {
        var added = addNode({
          id: ev.url, label: (ev.title || ev.url).slice(0, 40),
          type: 'FabricRecord',
          props: {url: ev.url, title: ev.title || '', dataset_id: ev.dataset_id},
        });
        if (added) {
          added._pulseUntil = Date.now() + 1500;
          if (ev.parent_url) {
            addEdge({from: ev.parent_url, to: ev.url, rel: 'LINKS_TO'});
          }
        }
      }
      if (ev.dataset_id && ev.stage === 'data_detected') {
        var added = addNode({
          id: ev.dataset_id,
          label: ev.dataset_id.split('.').slice(-2).join('.'),
          type: 'Dataset',
          props: {id: ev.dataset_id, kind: ev.kind || ''},
        });
        if (added) added._pulseUntil = Date.now() + 1500;
      }
    }

    function hideDetail(){
      if (detailEl) detailEl.style.display = 'none';
      state.selected = null;
    }

    // ── Expand / collapse ────────────────────────────────────────────────
    async function expandNode(node){
      if (state.expanded[node.id] && state.expanded[node.id].length) return;
      try {
        var added = [];
        var existing = {};
        state.nodes.forEach(function(n){ existing[n.id] = true; });

        function _ingest(dataNodes, dataEdges, defaultRel) {
          (dataNodes || []).forEach(function(n){
            if (existing[n.id]) return;
            var mc = (n.props && n.props.mention_count) || 1;
            var lbl = n.name || '';
            if ((!lbl || lbl === n.id) && n.props) {
              lbl = n.props.title || n.props.name || n.props.url || '';
              if (!lbl && n.props.text) lbl = n.props.text.split('\n')[0];
            }
            if (!lbl) lbl = n.id;
            var nt = n.type || (n.labels && n.labels[0]) || 'Node';
            var nn = {
              id: n.id, label: String(lbl).slice(0, 50), type: nt,
              props: n.props || {},
              x: node.x + (Math.random() - 0.5) * 140,
              y: node.y + (Math.random() - 0.5) * 140,
              vx: 0, vy: 0,
              r: nt === 'Dataset' ? 14 : nt === 'Entity' ? Math.max(5, Math.min(14, 4 + Math.sqrt(mc) * 2.5)) : 8,
            };
            state.nodes.push(nn); state.nodeIndex[n.id] = nn;
            existing[n.id] = true; added.push(n.id);
          });
          (dataEdges || []).forEach(function(e){
            if (existing[e.from] && existing[e.to]) {
              var dup = state.edges.some(function(x){ return x.from===e.from && x.to===e.to && x.rel===e.rel; });
              if (!dup) state.edges.push({ from: e.from, to: e.to, rel: e.rel || defaultRel, props: e.props || {} });
            }
          });
          // Ensure expanded node connects to children
          added.forEach(function(cid){
            var has = state.edges.some(function(e){ return (e.from===node.id && e.to===cid) || (e.from===cid && e.to===node.id); });
            if (!has) state.edges.push({ from: node.id, to: cid, rel: defaultRel, props: {} });
          });
        }

        if (node.type === 'Dataset') {
          // Expand dataset → its records with real titles from /fabric/browse
          var br = await fetch(apiBase + '/fabric/browse', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ dataset_id: node.id, limit: 80, offset: 0, search: '', lite: false }),
          });
          var bd = await br.json();
          var recs = (bd && bd.records) || [];
          if (recs.length) {
            var rn = recs.map(function(r){
              var t = r.title || r.name || (r.data && (r.data.title || r.data.name)) || (r.text||'').slice(0,80) || r.id;
              var u = r.url || r.link || (r.data && (r.data.url || r.data.link)) || '';
              return { id: r.id, name: t, type: 'FabricRecord',
                props: { title: t, url: u, dataset_id: node.id, text_preview: (r.text||'').slice(0,200),
                         tags: r.tags || (r.data && r.data.tags) || [] }};
            });
            // Fetch inter-record edges (RELATED_TO, LINKS_TO)
            var re2 = [];
            try {
              var sr = await fetch(apiBase + '/fabric/graphs/snapshot?graph=fabric&limit=500&dataset_id=' + encodeURIComponent(node.id));
              var sd = await sr.json();
              if (sd && sd.edges) re2 = sd.edges;
            } catch(e){}
            _ingest(rn, re2, 'CONTAINS');
          }
        } else if (node.type === 'FabricRecord') {
          // Expand record → entities mentioned in THIS specific record
          var rid = node.id;
          var er, ed;
          try {
            er = await fetch(apiBase + '/fabric/entity_graph/record_entities?record_id=' + encodeURIComponent(rid) + '&limit=60');
            ed = await er.json();
          } catch(e){ ed = null; }
          if (!ed || ed.error || !ed.nodes || !ed.nodes.length) {
            var dsid = (node.props && node.props.dataset_id) || '';
            if (dsid) {
              er = await fetch(apiBase + '/fabric/entity_graph/snapshot?limit=100&dataset_id=' + encodeURIComponent(dsid));
              ed = await er.json();
              if (ed && ed.nodes) {
                ed.nodes = ed.nodes.filter(function(n){ var rids = (n.props && n.props.record_ids) || []; return rids.indexOf(rid) >= 0; });
              }
            }
          }
          if (ed && ed.nodes && ed.nodes.length) _ingest(ed.nodes, ed.edges, 'MENTIONED_IN');
        } else if (node.type === 'Entity') {
          // Entity expansion strategy:
          //   1. Find records that mention this entity (MENTIONED_IN reverse)
          //   2. Find co-occurring entities in the same records
          var er2, ed2;
          // Try entity_graph snapshot with mentions for this specific entity
          try {
            er2 = await fetch(apiBase + '/fabric/entity_graph/snapshot?include_records=1&limit=120&entity_id=' + encodeURIComponent(node.id));
            ed2 = await er2.json();
          } catch(e){ ed2 = null; }
          // Fallback: query Neo4j directly for MENTIONED_IN + CO_OCCURS
          if (!ed2 || !ed2.nodes || !ed2.nodes.length) {
            try {
              var qr = await fetch(apiBase + '/fabric/graph/query', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({cypher:
                  'MATCH (e:Entity {id:$eid}) ' +
                  'OPTIONAL MATCH (e)-[m:MENTIONED_IN|HAS_ENTITY]-(r:FabricRecord) ' +
                  'OPTIONAL MATCH (e)-[c:CO_OCCURS|RELATED_TO]-(e2:Entity) ' +
                  'RETURN e, m, r, c, e2 LIMIT 80', params: {eid: node.id}})
              });
              ed2 = await qr.json();
            } catch(e2){ ed2 = null; }
          }
          if (ed2 && ed2.nodes && ed2.nodes.length) {
            _ingest(ed2.nodes, ed2.edges, 'MENTIONED_IN');
          }
        } else if (node.type === 'Ontology') {
          // Expand ontology → its OntologyEntity nodes via DEFINES edges
          try {
            var or1 = await fetch(apiBase + '/fabric/graph/query', {
              method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({cypher:
                'MATCH (o:Ontology {id:$oid})-[:DEFINES]->(e:OntologyEntity) ' +
                'OPTIONAL MATCH (e)-[r]->(e2:OntologyEntity) WHERE (o)-[:DEFINES]->(e2) ' +
                'RETURN e, r, e2 LIMIT 200', params: {oid: node.id}})
            });
            var od = await or1.json();
            var ontNodes = [], ontEdges = [];
            var seenOnt = {};
            (od.nodes || []).forEach(function(n){
              if (seenOnt[n.id]) return;
              seenOnt[n.id] = true;
              ontNodes.push({id: n.id, name: n.name || n.id, type: 'OntologyEntity',
                labels: n.labels, props: n.props || {}});
            });
            (od.edges || []).forEach(function(e){
              ontEdges.push({from: e.from, to: e.to, rel: e.rel, props: e.props || {}});
            });
            _ingest(ontNodes, ontEdges, 'DEFINES');
          } catch(e){ console.warn('ontology expand:', e); }
        } else if (node.type === 'Skill') {
          // Expand skill → its Concept nodes via HAS_CONCEPT edges
          try {
            var sr1 = await fetch(apiBase + '/fabric/graph/query', {
              method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({cypher:
                'MATCH (s:Skill {id:$sid})-[:HAS_CONCEPT]->(c:Concept) ' +
                'OPTIONAL MATCH (c)-[r]->(c2:Concept) WHERE (s)-[:HAS_CONCEPT]->(c2) ' +
                'RETURN c, r, c2 LIMIT 200', params: {sid: node.id}})
            });
            var skd = await sr1.json();
            var skNodes = [], skEdges = [];
            var seenSk = {};
            (skd.nodes || []).forEach(function(n){
              if (seenSk[n.id]) return; seenSk[n.id] = true;
              skNodes.push({id: n.id, name: n.name || n.id, type: 'Concept',
                labels: n.labels, props: n.props || {}});
            });
            (skd.edges || []).forEach(function(e){
              skEdges.push({from: e.from, to: e.to, rel: e.rel, props: e.props || {}});
            });
            _ingest(skNodes, skEdges, 'HAS_CONCEPT');
          } catch(e){ console.warn('skill expand:', e); }
        } else if (node.type === 'OntologyEntity' || node.type === 'Concept') {
          // Generic: try 1-hop Neo4j neighbours
          try {
            var gr = await fetch(apiBase + '/fabric/graph/query', {
              method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({cypher:
                'MATCH (a {id:$nid})-[r]-(b) RETURN a, r, b LIMIT 80',
                params: {nid: node.id}})
            });
            var gd = await gr.json();
            if (gd && gd.nodes && gd.nodes.length) {
              _ingest(gd.nodes, gd.edges, 'RELATED_TO');
            }
          } catch(e){}
        }

        state.expanded[node.id] = added;
        if (added.length) wake();
        if (state.selected && state.selected.id === node.id) showDetail(node);
        if (opts.onExpand) opts.onExpand(node, added, instance);
      } catch (e) { console.warn('expandNode', e); }
    }
    var expandEntities = expandNode;

    function collapseNode(node){
      var children = state.expanded[node.id] || [];
      if (!children.length) return;
      var keep = {};
      state.nodes.forEach(function(n){ keep[n.id] = true; });
      children.forEach(function(cid){
        var stillReferenced = false;
        Object.keys(state.expanded).forEach(function(srcId){
          if (srcId === node.id) return;
          if ((state.expanded[srcId] || []).indexOf(cid) >= 0) stillReferenced = true;
        });
        if (!stillReferenced) keep[cid] = false;
      });
      state.nodes = state.nodes.filter(function(n){ return keep[n.id]; });
      state.edges = state.edges.filter(function(e){ return keep[e.from] !== false && keep[e.to] !== false; });
      state.nodeIndex = {};
      state.nodes.forEach(function(n){ state.nodeIndex[n.id] = n; });
      delete state.expanded[node.id];
      wake();
      if (state.selected && state.selected.id === node.id) showDetail(node);
      if (opts.onCollapse) opts.onCollapse(node, instance);
    }

    async function expandRecords(node){
      var extra = detailEl ? detailEl.querySelector('.vg-detail-extra') : null;
      if (!extra) return;
      extra.innerHTML = '<span style="color:var(--dim2);font-size:9.5px">Loading records…</span>';
      try {
        var dsId = node.id;
        if (node.type === 'FabricRecord') dsId = (node.props && node.props.dataset_id) || '';
        if (!dsId && node.type === 'Entity') {
          var dsList = (node.props && node.props.datasets) || [];
          if (Array.isArray(dsList) && dsList.length) dsId = dsList[0];
        }
        if (!dsId) {
          extra.innerHTML = '<span style="color:var(--dim);font-size:9.5px">No dataset to browse</span>';
          return;
        }
        var res = await fetch(apiBase + '/fabric/browse', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dataset_id: dsId, limit: 8, offset: 0, search: '' })
        });
        var data = await res.json();
        var recs = (data && data.records) || [];
        if (!recs.length) {
          extra.innerHTML = '<span style="color:var(--dim);font-size:9.5px">No records</span>';
          return;
        }
        extra.innerHTML = '<div style="font-size:9px;color:var(--dim2);margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px">Recent Records</div>' +
          recs.map(function(r){
            var title = r.title || r.name || (r.text || '').slice(0, 60) || r.id || '';
            var url = r.url || r.link || '';
            return '<div style="padding:4px 6px;background:var(--bg2);border:1px solid var(--border);border-radius:3px;margin-bottom:3px">' +
              '<div style="font-size:9.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
              (url ? '<a href="' + esc(url) + '" target="_blank" style="color:var(--acc);text-decoration:none;margin-right:4px">↗</a>' : '') +
              esc(title.slice(0, 70)) + '</div></div>';
          }).join('');
      } catch (e) {
        extra.innerHTML = '<span style="color:var(--err);font-size:9.5px">Load failed: ' + esc(e.message || e) + '</span>';
      }
    }

    // ── Layers / groups (structural vs informational, etc.) ───────────────
    // Every node and edge belongs to a layer. A layer can have its visibility
    // and/or physics toggled independently. Layer assignment priority:
    //   1. explicit `layer` (or `group`) on the node/edge data
    //   2. inferred from the node type / edge rel via the maps below
    //   3. fallback 'default'
    // The inference maps are intentionally data-driven (not hard-coded into the
    // logic) so they can be extended or overridden via opts.layerMap without
    // touching the engine. With NO layer data and NO map override, everything
    // lands in 'default' and the graph behaves exactly as before (drop-in safe).
    var _DEFAULT_NODE_LAYER_MAP = {
      // structural
      Dataset:'structural', Source:'structural', FabricRecord:'structural',
      Page:'structural', Subtable:'structural', Work:'structural',
      Surface:'structural', Container:'structural', DockerHost:'structural',
      NetHost:'structural', SshHost:'structural', Subnet:'structural',
      NetService:'structural', DAG:'structural', Skill:'structural',
      Category:'structural', Ontology:'structural',
      // informational
      Entity:'informational', Agent:'informational',
      // memory (cap-run trail + memories) — toggleable
      Memory:'memory', Session:'memory', CapResult:'informational',
      // worldview latent map
      WorldviewPoint:'worldview', Concept:'worldview'
    };
    var _DEFAULT_EDGE_LAYER_MAP = {
      // structural relationships
      CONTAINS:'structural', HAS_SURFACE:'structural', HAS_PAGE:'structural',
      HAS_SUBTABLE:'structural', WORK:'structural', HAS_WORK:'structural',
      MENTIONS:'structural', MENTIONED_IN:'structural', HAS_ENTITY:'structural',
      DEFINES:'structural', HAS_CONCEPT:'structural', PART_OF:'structural',
      // informational relationships (2nd-order entity graph)
      CO_OCCURS:'informational', RELATED_TO:'informational',
      SIMILAR_TO:'informational', REFERENCES:'informational',
      DEPENDS_ON:'informational', DERIVED_FROM:'informational',
      SHARES_TOPIC:'informational', LINKS_TO:'informational',
      // memory-trail edges
      REMEMBERS:'memory', CONTEXT:'informational'
    };
    var _nodeLayerMap = Object.assign({}, _DEFAULT_NODE_LAYER_MAP, (opts.layerMap && opts.layerMap.nodes) || {});
    var _edgeLayerMap = Object.assign({}, _DEFAULT_EDGE_LAYER_MAP, (opts.layerMap && opts.layerMap.edges) || {});

    function _nodeLayer(spec){
      var l = spec.layer || spec.group ||
              (spec.props && (spec.props.layer || spec.props.group));
      if (l) return String(l);
      return _nodeLayerMap[spec.type] ||
             (spec.labels && _nodeLayerMap[spec.labels[0]]) || 'default';
    }
    function _edgeLayer(spec){
      var l = spec.layer || spec.group ||
              (spec.props && (spec.props.layer || spec.props.group));
      if (l) return String(l);
      return _edgeLayerMap[spec.rel] || _edgeLayerMap[spec.label] || 'default';
    }
    // Ensure a layer is registered in state with default on/on. Returns the rec.
    function _ensureLayer(name){
      state.layers = state.layers || {};
      if (!state.layers[name]) state.layers[name] = { visible: true, physics: true };
      return state.layers[name];
    }
    function _layerVisible(name){ var l = state.layers && state.layers[name]; return !l || l.visible !== false; }
    function _layerPhysics(name){ var l = state.layers && state.layers[name]; return !l || l.physics !== false; }

    // ── Cluster detection (light label-propagation community finding) ─────
    // Computes, for every node:
    //   n._cluster  — id of the community it belongs to
    //   n._bridge   — how many DISTINCT clusters it connects to (1 = lives in
    //                 a single cluster; higher = a connector spanning many)
    // Used by the force layout to give bridge nodes longer edges and to push
    // whole clusters apart. Cheap: a handful of propagation passes, O(E) each.
    function _computeClusters(){
      var nodes = state.nodes, edges = state.edges;
      if (!nodes.length) return;
      // adjacency
      var adj = {};
      nodes.forEach(function(n){ adj[n.id] = []; });
      edges.forEach(function(e){
        if (adj[e.from] && adj[e.to]) { adj[e.from].push(e.to); adj[e.to].push(e.from); }
      });
      // init: each node its own label
      var label = {};
      nodes.forEach(function(n){ label[n.id] = n.id; });
      // label propagation: each node takes the most common label among neighbours
      var passes = Math.min(8, 3 + Math.floor(Math.sqrt(nodes.length) / 4));
      for (var p = 0; p < passes; p++) {
        var changed = false;
        // shuffle order a little for stability
        for (var i = 0; i < nodes.length; i++) {
          var id = nodes[i].id, nbrs = adj[id];
          if (!nbrs.length) continue;
          var counts = {}, best = label[id], bestC = 0;
          for (var j = 0; j < nbrs.length; j++) {
            var l = label[nbrs[j]];
            counts[l] = (counts[l] || 0) + 1;
            if (counts[l] > bestC) { bestC = counts[l]; best = l; }
          }
          if (best !== label[id]) { label[id] = best; changed = true; }
        }
        if (!changed) break;
      }
      // assign cluster + compute how many distinct clusters each node touches
      nodes.forEach(function(n){
        n._cluster = label[n.id];
        var seen = {}, count = 0;
        var nbrs = adj[n.id];
        for (var k = 0; k < nbrs.length; k++) {
          var cl = label[nbrs[k]];
          if (!seen[cl]) { seen[cl] = 1; count++; }
        }
        // include own cluster in the span if it has any neighbours
        if (!seen[n._cluster] && nbrs.length) count++;
        n._bridge = Math.max(1, count);
      });
      // cache the set of cluster ids for cluster-level separation
      var cset = {};
      nodes.forEach(function(n){ cset[n._cluster] = 1; });
      state._clusterIds = Object.keys(cset);
    }

    // ── load / addNode / addEdge ─────────────────────────────────────────
    function load(data){
      data = data || {};
      // Any direct load() marks the view as NOT snapshot-sourced by default.
      // fetchSnapshot re-sets this to true immediately after its own load()
      // call, so only host-panel direct loads leave it false.
      state.loadedViaSnapshot = false;
      state._axisMode = false;
      // Preserve positions of nodes that persist across the reload, so callers
      // that re-feed a filtered/refreshed node set don't get a layout reset.
      var prevPos = {};
      state.nodes.forEach(function(n){ prevPos[n.id] = { x:n.x, y:n.y, vx:n.vx, vy:n.vy }; });
      state.nodes = []; state.edges = []; state.nodeIndex = {};
      state.expanded = {}; state.searchHighlight = new Set();
      state.tickCount = 0; state.frozen = false;
      (data.nodes || []).forEach(function(n){
        var added = addNode(n, true);
        if (added && prevPos[added.id] && n.x === undefined && n.y === undefined) {
          var pp = prevPos[added.id];
          added.x = pp.x; added.y = pp.y; added.vx = pp.vx || 0; added.vy = pp.vy || 0;
        }
      });
      (data.edges || []).forEach(function(e){ addEdge(e, true); });
      // Nodes that spawned at the periphery but have NO edges have no spring to
      // pull them inward — they'd stay stranded at the edge. Move any such
      // isolated, freshly-placed node to near the centre instead. (We only move
      // nodes that weren't given explicit coords and weren't position-preserved.)
      var _connected = {};
      state.edges.forEach(function(e){ _connected[e.from] = 1; _connected[e.to] = 1; });
      var _cx = (W || 640) / 2, _cy = (H || 420) / 2;
      state.nodes.forEach(function(n){
        if (!_connected[n.id] && n._spawnedAtEdge && !prevPos[n.id]) {
          n.x = _cx + (Math.random() - 0.5) * 160;
          n.y = _cy + (Math.random() - 0.5) * 160;
          n.vx = 0; n.vy = 0;
        }
      });
      _computeClusters();
      _applyVis();
      // Give the layout several automatic anneal cycles so it settles into a
      // good arrangement on its own (equivalent to pressing Re-layout a few
      // times). More nodes => allow a couple more cycles.
      state.settleCycles = state.nodes.length > 120 ? 5 : 3;
      updateMeta(); updateLegend(); _rebuildChips(); _rebuildTags(); _updateDebug(); _renderLayerUI(); wake();
    }

    // Spawn a node PART-WAY out from the centre (about halfway to the current
    // cloud's edge) rather than at the far periphery. Starting closer in means
    // the springs have far less distance to reel nodes in, so the layout settles
    // to its resting state much faster, while still avoiding a dense central pile.
    function _spawnAtEdge(){
      var cx = (W || 640) / 2, cy = (H || 420) / 2;
      if (!state.nodes.length) {
        // first node(s): small jitter around centre
        return { x: cx + (Math.random() - 0.5) * 120, y: cy + (Math.random() - 0.5) * 120 };
      }
      // find centroid + max radius of existing nodes
      var mx = 0, my = 0, k = 0;
      for (var i = 0; i < state.nodes.length; i++) { mx += state.nodes[i].x; my += state.nodes[i].y; k++; }
      mx /= k; my /= k;
      var maxR = 0;
      for (var j = 0; j < state.nodes.length; j++) {
        var ddx = state.nodes[j].x - mx, ddy = state.nodes[j].y - my;
        var rr = Math.sqrt(ddx * ddx + ddy * ddy);
        if (rr > maxR) maxR = rr;
      }
      // ~halfway between centre and the cloud edge (with a little jitter)
      var ring = maxR * 0.5 + 40 + Math.random() * 40;
      var a = Math.random() * Math.PI * 2;
      return { x: mx + Math.cos(a) * ring, y: my + Math.sin(a) * ring };
    }

    function addNode(nodeSpec, silent){
      if (!nodeSpec || !nodeSpec.id) return null;
      if (state.nodeIndex[nodeSpec.id]) return state.nodeIndex[nodeSpec.id];
      var lbl = nodeSpec.label || nodeSpec.name || '';
      if ((!lbl || lbl === nodeSpec.id) && nodeSpec.props) {
        var p = nodeSpec.props;
        lbl = p.title || p.name || p.label || p.url || '';
        if (!lbl && p.text) lbl = p.text.split('\n')[0];
      }
      if (!lbl) lbl = nodeSpec.id;
      var _usedEdgeSpawn = (nodeSpec.x === undefined && nodeSpec.y === undefined);
      var _spawn = _spawnAtEdge();
      var n = {
        id:    nodeSpec.id,
        label: String(lbl).slice(0, 50),
        type:  nodeSpec.type || (nodeSpec.labels && nodeSpec.labels[0]) || 'Node',
        props: nodeSpec.props || {},
        x: nodeSpec.x !== undefined ? nodeSpec.x : _spawn.x,
        y: nodeSpec.y !== undefined ? nodeSpec.y : _spawn.y,
        vx: 0, vy: 0,
        r: nodeSpec.r || (nodeSpec.type === 'Entity' ? 8 : nodeSpec.type === 'Dataset' ? 14 : 10),
      };
      n._spawnedAtEdge = _usedEdgeSpawn && !nodeSpec._fromId;
      n._layer = _nodeLayer(nodeSpec);
      _ensureLayer(n._layer);
      if (nodeSpec._fromId && state.nodeIndex[nodeSpec._fromId]) {
        var src = state.nodeIndex[nodeSpec._fromId];
        n.x = src.x + (Math.random() - 0.5) * 120;
        n.y = src.y + (Math.random() - 0.5) * 120;
      }
      state.nodes.push(n);
      state.nodeIndex[nodeSpec.id] = n;
      if (!silent) { wake(); updateMeta(); updateLegend(); _renderLayerUI(); }
      return n;
    }

    function addEdge(edgeSpec, silent){
      if (!edgeSpec || !edgeSpec.from || !edgeSpec.to) return;
      for (var i = 0; i < state.edges.length; i++) {
        var e = state.edges[i];
        if (e.from === edgeSpec.from && e.to === edgeSpec.to && e.rel === edgeSpec.rel) return;
      }
      var _e = {
        from: edgeSpec.from, to: edgeSpec.to,
        rel:  (edgeSpec.rel || edgeSpec.label || '').slice(0, 20),
        props: edgeSpec.props || {},
      };
      if (edgeSpec._dashed || edgeSpec.dashed) _e._dashed = true;
      _e._layer = _edgeLayer(edgeSpec);
      _ensureLayer(_e._layer);
      state.edges.push(_e);
      if (!silent) { wake(); updateMeta(); _renderLayerUI(); }
    }

    function updateMeta(){
      if (!metaEl) return;
      metaEl.textContent = state.nodes.length + ' nodes, ' + state.edges.length + ' edges';
    }

    function updateLegend(){
      if (!legendEl) return;
      var byType = {};
      state.nodes.forEach(function(n){ byType[n.type || '?'] = true; });
      legendEl.innerHTML = Object.keys(byType).map(function(t){
        return '<span style="display:inline-flex;align-items:center;gap:3px"><span style="width:8px;height:8px;border-radius:50%;background:' + (COL[t] || '#888') + '"></span>' + esc(t) + '</span>';
      }).join('');
    }

    // ── Backend snapshot loader ───────────────────────────────────────────
    async function fetchSnapshot(layer, params){
      params = params || {};
      state.currentLayer  = layer || state.currentLayer;
      state.currentParams = params;
      var qs = [];
      var url;
      if (layer === 'memory') {
        return fetchMemory(params.memoryMode || 'all', params);
      }
      if (layer === 'entity') {
        url = '/fabric/entity_graph/snapshot';
        if (params.dataset_id)  qs.push('dataset_id=' + encodeURIComponent(params.dataset_id));
        if (params.entity_type) qs.push('entity_type=' + encodeURIComponent(params.entity_type));
        qs.push('limit=' + (params.limit || 300));
        if (params.include_datasets) qs.push('include_datasets=1');
        if (params.include_records)  qs.push('include_records=1');
      } else {
        url = '/fabric/graphs/snapshot';
        qs.push('graph=' + encodeURIComponent(layer || 'fabric'));
        qs.push('limit=' + (params.limit || 200));
        if (params.dataset_id)   qs.push('dataset_id=' + encodeURIComponent(params.dataset_id));
        if (params.label_filter) qs.push('label_filter=' + encodeURIComponent(params.label_filter));
      }
      try {
        var res = await fetch(apiBase + url + '?' + qs.join('&'));
        var data = await res.json();
        if (data && data.nodes) {
          var nodes = data.nodes.map(function(n){
            var nodeType = n.type || (n.labels && n.labels[0]) || 'Node';
            var mc = (n.props && n.props.mention_count) || 1;
            return {
              id:    n.id,
              label: (n.name || (n.props && (n.props.title || n.props.name || n.props.url)) || n.label || n.id || '').slice(0, 50),
              type:  nodeType,
              props: n.props || {},
              r: nodeType === 'Dataset' ? 14 :
                 nodeType === 'FabricRecord' ? 8 :
                 nodeType === 'Entity' ? Math.max(6, Math.min(20, 5 + Math.sqrt(mc) * 3)) : 10,
            };
          });
          load({ nodes: nodes, edges: data.edges || [] });
          // Mark AFTER load() (which clears the flag) so the post-action
          // refetch knows this view came from a snapshot and is safe to reload.
          state.loadedViaSnapshot = true;
        }
      } catch (e) {
        console.warn('fetchSnapshot', layer, e);
      }
    }

    // ── Live event subscription (always-on, even before any action runs)
    // This makes the graph naturally pick up streaming events from
    // long-running operations (web acquisition, Loom, extract) without
    // the host panel needing to wire anything.
    var _liveUnsubs = [];
    if (opts.subscribeLiveEvents !== false) {
      var bus = opts.eventBus || _getSharedBus();
      if (bus) {
        var prefixes = opts.livePrefixes || [
          'fabric.web.acquire.progress',
          'fabric.entity_graph.progress',
          'fabric.loom.progress',
          'fabric.unified_run.progress',
          'fabric.record.ingested',
        ];
        prefixes.forEach(function(p){
          _liveUnsubs.push(bus.subscribe(p, function(ev){
            _forwardEventToGraph(ev);
          }));
        });
      }
    }

    // ── Cap runner wiring ────────────────────────────────────────────────
    var _capCache = null;
    async function _loadCaps(){
      if(_capCache)return _capCache;
      try{var res=await fetch(apiBase+'/mcp/tools');_capCache={};var list=await res.json();(list||[]).forEach(function(c){_capCache[c.name]=c;});}catch(e){_capCache={};}
      return _capCache;
    }
    async function _wireCapRunner(el, node){
      var sel=el.querySelector('.vg-cap-sel'),par=el.querySelector('.vg-cap-params'),runBtn=el.querySelector('.vg-cap-run'),resEl=el.querySelector('.vg-cap-result');
      if(!sel)return;
      var caps=await _loadCaps();
      sel.innerHTML='<option value="">\u2014 pick capability \u2014</option>'+Object.keys(caps).sort().map(function(n){return '<option value="'+esc(n)+'">'+esc(n)+'</option>';}).join('');
      sel.onchange=function(){
        var cap=caps[sel.value];if(!cap){par.innerHTML='';return;}
        var props=(cap.schema&&cap.schema.properties)||{};
        par.innerHTML=Object.keys(props).filter(function(k){return k!=='trace_id';}).map(function(k){
          var v=props[k]; var prefill=(node.props&&node.props[k])||'';
          return '<div style="display:flex;gap:4px;align-items:center"><span style="font-family:var(--mono,monospace);font-size:7.5px;color:var(--dim,#6a6058);min-width:50px;flex-shrink:0">'+esc(k)+'</span><input data-cp="'+esc(k)+'" value="'+esc(prefill)+'" placeholder="'+esc(v.type||k)+'" style="flex:1;font-size:8.5px;padding:2px 4px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:2px;font-family:var(--mono,monospace)" ondragover="event.preventDefault()" ondrop="event.preventDefault();this.value=event.dataTransfer.getData(\'text/plain\').slice(0,500)"></div>';
        }).join('');
      };
      if(runBtn) runBtn.onclick=async function(){
        if(!sel.value)return;
        if(resEl){resEl.style.display='block';resEl.textContent='running...';resEl.style.color='var(--dim,#6a6058)';}
        var args={};par.querySelectorAll('[data-cp]').forEach(function(el2){if(el2.value)args[el2.dataset.cp]=el2.value;});
        var capName = sel.value;

        // Record this capability run to the memory layer (same as actions do)
        var fakeAction = { id: capName, capability: capName };
        var memNodeId = _recordActivity(fakeAction, node, 'running');

        try{
          var r=await fetch(apiBase+'/mcp/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:capName,arguments:args})});
          var d=await r.json();
          var c=d.content||d;
          if(resEl){resEl.textContent=(typeof c==='string'?c:JSON.stringify(c,null,2)).slice(0,2000);resEl.style.color='var(--ok,#6db87a)';}

          // Graph the results — parse them into nodes linked to the source node,
          // same as _graphActionResults does for actions.
          try {
            var payload = (typeof c === 'string') ? (function(){ try{return JSON.parse(c);}catch(e){return {};} })() : (c || {});
            _graphActionResults(fakeAction, node, { ok: true, result: payload });
          } catch(e2) {
            if (typeof console !== 'undefined') console.warn('[vera-graph] cap result graphing:', e2);
          }

          // Update memory node status
          try { _updateActivity(memNodeId, 'done', { ok: true, result: typeof c === 'object' ? c : {} }); } catch(e3){}

        }catch(e){
          if(resEl){resEl.textContent='Error: '+e.message;resEl.style.color='var(--err,#c96b6b)';}
          try { _updateActivity(memNodeId, 'error'); } catch(e4){}
        }
      };
    }

    // ── Memory graph fetch ───────────────────────────────────────────────
    async function fetchMemory(mode, params){
      params = params || {};
      var qs = 'mode='+(mode||'session')+'&limit_nodes='+(params.limit||500)+'&limit_edges='+(params.edgeLimit||3000);
      if(params.session_id) qs += '&session_id='+encodeURIComponent(params.session_id);
      if(mode==='recent') qs += '&recent_hours='+(params.hours||24);
      try{
        var res=await fetch(apiBase+'/memory/graph/full?'+qs);
        var data=await res.json();
        if(!data||data.error){console.warn('fetchMemory:',data&&data.error);return;}
        var nodes=(data.nodes||[]).map(function(n){return{id:n.id,label:(n.summary||n.text||n.capability||n.id||'').slice(0,50),type:n.record_type||'Memory',props:n,r:8+parseFloat(n.importance||0.5)*8};});
        var edges=(data.edges||[]).map(function(e){return{from:e.from_id||e.from,to:e.to_id||e.to,rel:e.relation||'RELATED'};});
        load({nodes:nodes,edges:edges});
      }catch(e){console.warn('fetchMemory:',e);}
    }

    // ── Built-in drawer sections (opt-in via opts.sections array) ──────
    // Available: 'view','actions','linkSuggestions','manualLink',
    //            'cypherComposer','cypherQuery','registeredGraphs','registerGraph'
    var _sectionHTML = {
      view:
        '<div style="padding-top:8px;border-top:1px solid var(--border,#3a3530);margin-top:10px">' +
        '<div style="font-size:8px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">View Controls</div>' +
        '<div style="display:flex;gap:4px;align-items:center;margin-bottom:4px"><label style="font-size:9px;color:var(--dim2,#8a7e70);min-width:40px">Graph</label><select class="vg-sec-graph-picker" style="flex:1;font-size:9px;padding:2px 4px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:3px"><option value="fabric">fabric</option></select></div>' +
        '<div style="display:flex;gap:4px;align-items:center;margin-bottom:4px"><label style="font-size:9px;color:var(--dim2,#8a7e70);min-width:40px">Filter</label><select class="vg-sec-filter" style="flex:1;font-size:9px;padding:2px 4px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:3px"><option value="all">All nodes</option><option value="datasets">Datasets only</option><option value="sources">Sources \u2192 Datasets</option></select></div>' +
        '<label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:9px;color:var(--dim2,#8a7e70)"><input type="checkbox" class="vg-sec-autostitch"><span>Auto-stitch (Loom)</span></label></div>',
      fabricActions:
        '<div style="padding-top:8px;border-top:1px solid var(--border,#3a3530);margin-top:10px">' +
        '<div style="font-size:8px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Fabric Actions</div>' +
        '<div style="display:flex;gap:4px;flex-wrap:wrap">' +
          '<button class="vg-sec-ai-links" style="font-size:8.5px;padding:2px 7px;background:rgba(90,158,143,.1);border:1px solid var(--acc,#5a9e8f);color:var(--acc,#5a9e8f);border-radius:3px;cursor:pointer">AI Analyse Links</button>' +
          '<button class="vg-sec-auto-link" style="font-size:8.5px;padding:2px 7px;background:var(--bg2,#272421);border:1px solid var(--border,#3a3530);color:var(--dim2,#8a7e70);border-radius:3px;cursor:pointer">Auto-link</button>' +
        '</div></div>',
      linkSuggestions:
        '<div class="vg-sec-linksug" style="display:none;padding-top:8px;border-top:1px solid var(--border,#3a3530);margin-top:10px">' +
        '<div style="font-size:8px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Link Suggestions</div>' +
        '<div style="display:flex;gap:4px;align-items:center;margin-bottom:4px"><label style="font-size:9px;color:var(--dim2,#8a7e70)">Min score: <input class="vg-sec-threshold" type="number" value="50" min="0" max="100" style="width:40px;font-size:9px;padding:1px 3px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:2px">%</label><button class="vg-sec-apply-all" style="font-size:8.5px;padding:2px 6px;background:rgba(90,158,143,.1);border:1px solid var(--acc,#5a9e8f);color:var(--acc,#5a9e8f);border-radius:3px;cursor:pointer">Apply All</button></div>' +
        '<div class="vg-sec-suglist" style="font-size:9px"></div></div>',
      manualLink:
        '<div style="padding-top:8px;border-top:1px solid var(--border,#3a3530);margin-top:10px">' +
        '<div style="font-size:8px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Link From This Node</div>' +
        '<div style="display:flex;gap:4px;align-items:center;margin-bottom:4px">' +
          '<input class="vg-sec-link-to" placeholder="Target node ID..." style="flex:1;font-size:9px;padding:2px 5px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:2px;font-family:var(--mono,monospace)" >' +
          '<select class="vg-sec-link-rel" style="font-size:9px;padding:2px 4px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:2px"><option>RELATED_TO</option><option>SIMILAR_TO</option><option>DERIVED_FROM</option><option>COMPLEMENTS</option><option>SHARES_TOPIC</option></select></div>' +
        '<button class="vg-sec-link-btn" style="font-size:8.5px;padding:2px 8px;background:var(--bg2,#272421);border:1px solid var(--border,#3a3530);color:var(--dim2,#8a7e70);border-radius:3px;cursor:pointer">Create Link</button>' +
        '<div class="vg-sec-link-status" style="font-size:9px;color:var(--dim,#6a6058);margin-top:3px"></div></div>',
      cypherComposer:
        '<div style="padding-top:8px;border-top:1px solid var(--border,#3a3530);margin-top:10px">' +
        '<div style="font-size:8px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Cypher Composer</div>' +
        '<div style="display:flex;gap:4px;margin-bottom:4px"><select class="vg-sec-cyp-from" style="flex:1;font-size:9px;padding:2px 3px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:2px"><option value="">(from)</option><option>Dataset</option><option>FabricRecord</option><option>Source</option><option>Entity</option><option>Memory</option></select>' +
        '<select class="vg-sec-cyp-rel" style="flex:1;font-size:9px;padding:2px 3px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:2px"><option value="">(rel)</option><option>RELATED_TO</option><option>CONTAINS</option><option>HAS_ENTITY</option><option>MENTIONS</option><option>DERIVED_FROM</option></select>' +
        '<select class="vg-sec-cyp-to" style="flex:1;font-size:9px;padding:2px 3px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:2px"><option value="">(to)</option><option>Dataset</option><option>FabricRecord</option><option>Source</option><option>Entity</option><option>Memory</option></select></div>' +
        '<div style="display:flex;gap:4px;align-items:center"><input class="vg-sec-cyp-limit" type="number" value="100" min="1" max="2000" style="width:50px;font-size:9px;padding:2px 3px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:2px"><button class="vg-sec-cyp-run" style="font-size:8.5px;padding:2px 6px;background:rgba(90,158,143,.1);border:1px solid var(--acc,#5a9e8f);color:var(--acc,#5a9e8f);border-radius:3px;cursor:pointer">Run</button></div></div>',
      cypherQuery:
        '<div style="padding-top:8px;border-top:1px solid var(--border,#3a3530);margin-top:10px">' +
        '<div style="font-size:8px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Cypher Query</div>' +
        '<textarea class="vg-sec-cypher" style="width:100%;height:55px;font-family:var(--mono,monospace);font-size:9px;padding:4px 6px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:3px;resize:vertical" placeholder="MATCH (d:Dataset)-[r]->(m) RETURN d,type(r),m LIMIT 20"></textarea>' +
        '<button class="vg-sec-cypher-run" style="margin-top:4px;font-size:8.5px;padding:2px 8px;background:var(--bg2,#272421);border:1px solid var(--border,#3a3530);color:var(--dim2,#8a7e70);border-radius:3px;cursor:pointer">Run</button>' +
        '<pre class="vg-sec-cypher-result" style="display:none;margin-top:4px;font-size:8.5px;font-family:var(--mono,monospace);background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);border-radius:3px;padding:5px 7px;max-height:140px;overflow:auto;white-space:pre-wrap;color:var(--dim2,#8a7e70)"></pre></div>',
      registeredGraphs:
        '<div style="padding-top:8px;border-top:1px solid var(--border,#3a3530);margin-top:10px">' +
        '<div style="font-size:8px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Registered Graphs</div>' +
        '<div class="vg-sec-graph-list" style="font-size:9px;color:var(--dim,#6a6058)">Loading...</div></div>',
      registerGraph:
        '<div style="padding-top:8px;border-top:1px solid var(--border,#3a3530);margin-top:10px">' +
        '<div style="font-size:8px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Register Graph</div>' +
        '<input class="vg-sec-reg-name" placeholder="name" style="width:100%;font-size:9px;padding:2px 5px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:2px;margin-bottom:3px;font-family:var(--mono,monospace)">' +
        '<input class="vg-sec-reg-labels" placeholder="Node,Labels,CSV" style="width:100%;font-size:9px;padding:2px 5px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:2px;margin-bottom:3px;font-family:var(--mono,monospace)">' +
        '<button class="vg-sec-reg-btn" style="font-size:8.5px;padding:2px 8px;background:var(--bg2,#272421);border:1px solid var(--border,#3a3530);color:var(--dim2,#8a7e70);border-radius:3px;cursor:pointer">Register</button>' +
        '<div class="vg-sec-reg-status" style="font-size:9px;color:var(--dim,#6a6058);margin-top:3px"></div></div>',
    };

    // Build drawerSections from opts.sections array + explicit opts.drawerSections
    var _builtSections = '';
    if (opts.sections && Array.isArray(opts.sections)) {
      opts.sections.forEach(function(name){ if (_sectionHTML[name]) _builtSections += _sectionHTML[name]; });
    }
    if (opts.drawerSections) _builtSections += opts.drawerSections;

    // Populate leftSections in the left panel (deferred because _sectionHTML is defined here)
    var _leftSecEl = container.querySelector('.vg-left-sections');
    if (_leftSecEl && opts.leftSections && Array.isArray(opts.leftSections)) {
      var lsHTML = '';
      opts.leftSections.forEach(function(name){ if (_sectionHTML[name]) lsHTML += _sectionHTML[name]; });
      _leftSecEl.innerHTML = lsHTML;
      // Wire left-panel section handlers immediately
      _wireLeftSections(_leftSecEl);
    }

    function _wireLeftSections(el){
      // Cypher query
      var cypRunBtn = el.querySelector('.vg-sec-cypher-run');
      if(cypRunBtn) cypRunBtn.onclick = async function(){
        var ta=el.querySelector('.vg-sec-cypher'), resEl=el.querySelector('.vg-sec-cypher-result');
        if(!ta||!ta.value.trim())return;
        try{var r=await fetch(apiBase+'/fabric/graph/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cypher:ta.value.trim()})});var d=await r.json();if(resEl){resEl.style.display='block';resEl.textContent=JSON.stringify(d.rows||d,null,2).slice(0,4000);}}catch(e){if(resEl){resEl.style.display='block';resEl.textContent='Error: '+e.message;}}
      };
      // Cypher composer
      var cypCompRun = el.querySelector('.vg-sec-cyp-run');
      if(cypCompRun) cypCompRun.onclick = async function(){
        var f=el.querySelector('.vg-sec-cyp-from'),r2=el.querySelector('.vg-sec-cyp-rel'),t=el.querySelector('.vg-sec-cyp-to'),lim=el.querySelector('.vg-sec-cyp-limit');
        var q='MATCH '; q+=(f&&f.value?'(a:'+f.value+')':'(a)'); q+=(r2&&r2.value?'-[:'+r2.value+']->' :'-[r]->'); q+=(t&&t.value?'(b:'+t.value+')':'(b)'); q+=' RETURN a,type(r),b LIMIT '+(lim?lim.value:'100');
        var ta=el.querySelector('.vg-sec-cypher');if(ta)ta.value=q;
        var btn=el.querySelector('.vg-sec-cypher-run');if(btn)btn.click();
      };
      // Register graph
      var regBtn = el.querySelector('.vg-sec-reg-btn');
      if(regBtn) regBtn.onclick = async function(){
        var nameEl=el.querySelector('.vg-sec-reg-name'),labelsEl=el.querySelector('.vg-sec-reg-labels'),statEl=el.querySelector('.vg-sec-reg-status');
        if(!nameEl||!nameEl.value.trim()){if(statEl)statEl.textContent='Name required';return;}
        try{var r=await fetch(apiBase+'/fabric/graphs/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:nameEl.value.trim(),node_labels:labelsEl?labelsEl.value.trim():''})});var d=await r.json();if(statEl)statEl.textContent=d&&d.name?'Registered: '+d.name:(d&&d.error||'Failed');}catch(e){if(statEl)statEl.textContent=e.message;}
      };
      // Load registered graphs list
      var graphList = el.querySelector('.vg-sec-graph-list');
      if(graphList){
        fetch(apiBase+'/fabric/graphs').then(function(r){return r.json();}).then(function(d){
          graphList.innerHTML = (d.graphs||[]).map(function(g){return '<div style="padding:2px 0">'+esc(g.name)+(g.user_registered?' <span style="color:var(--acc,#5a9e8f);font-size:8px">(custom)</span>':'')+'</div>';}).join('')||'None';
        }).catch(function(){graphList.textContent='Failed to load';});
      }
    }

    // Wire section handlers after each showDetail call (delegated to _wireSections)
    function _wireSections(detailEl, node){
      // Manual link - wire drag-drop on the target input
      var linkToEl = detailEl.querySelector('.vg-sec-link-to');
      if(linkToEl){
        linkToEl.ondragover = function(e){e.preventDefault();};
        linkToEl.ondrop = function(e){e.preventDefault();linkToEl.value=e.dataTransfer.getData('text/plain').slice(0,200);};
      }
      var linkBtn = detailEl.querySelector('.vg-sec-link-btn');
      if(linkBtn) linkBtn.onclick = async function(){
        var toEl=detailEl.querySelector('.vg-sec-link-to'), relEl=detailEl.querySelector('.vg-sec-link-rel'), statEl=detailEl.querySelector('.vg-sec-link-status');
        if(!toEl||!toEl.value)return;
        if(statEl)statEl.textContent='Linking...';
        try{var r=await fetch(apiBase+'/fabric/graph/link',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({from_type:node.type||'Dataset',from_id:node.id,to_type:'Dataset',to_id:toEl.value,rel:relEl?relEl.value:'RELATED_TO'})});var d=await r.json();if(statEl)statEl.textContent=d&&d.ok?'Linked':'Error: '+(d&&d.error||'?');if(d&&d.ok)addEdge({from:node.id,to:toEl.value,rel:relEl?relEl.value:'RELATED_TO'});}catch(e){if(statEl)statEl.textContent=e.message;}
      };
      // Cypher query
      var cypRunBtn = detailEl.querySelector('.vg-sec-cypher-run');
      if(cypRunBtn) cypRunBtn.onclick = async function(){
        var ta=detailEl.querySelector('.vg-sec-cypher'), resEl=detailEl.querySelector('.vg-sec-cypher-result');
        if(!ta||!ta.value.trim())return;
        try{var r=await fetch(apiBase+'/fabric/graph/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cypher:ta.value.trim()})});var d=await r.json();if(resEl){resEl.style.display='block';resEl.textContent=JSON.stringify(d.rows||d,null,2).slice(0,4000);}}catch(e){if(resEl){resEl.style.display='block';resEl.textContent='Error: '+e.message;}}
      };
      // Cypher composer
      var cypCompRun = detailEl.querySelector('.vg-sec-cyp-run');
      if(cypCompRun) cypCompRun.onclick = async function(){
        var f=detailEl.querySelector('.vg-sec-cyp-from'),r2=detailEl.querySelector('.vg-sec-cyp-rel'),t=detailEl.querySelector('.vg-sec-cyp-to'),lim=detailEl.querySelector('.vg-sec-cyp-limit');
        var q='MATCH '; q+=(f&&f.value?'(a:'+f.value+')':'(a)'); q+=(r2&&r2.value?'-[:'+r2.value+']->' :'-[r]->'); q+=(t&&t.value?'(b:'+t.value+')':'(b)'); q+=' RETURN a,type(r),b LIMIT '+(lim?lim.value:'100');
        var ta=detailEl.querySelector('.vg-sec-cypher');if(ta)ta.value=q;
        var btn=detailEl.querySelector('.vg-sec-cypher-run');if(btn)btn.click();
      };
      // Register graph
      var regBtn = detailEl.querySelector('.vg-sec-reg-btn');
      if(regBtn) regBtn.onclick = async function(){
        var nameEl=detailEl.querySelector('.vg-sec-reg-name'),labelsEl=detailEl.querySelector('.vg-sec-reg-labels'),statEl=detailEl.querySelector('.vg-sec-reg-status');
        if(!nameEl||!nameEl.value.trim()){if(statEl)statEl.textContent='Name required';return;}
        try{var r=await fetch(apiBase+'/fabric/graphs/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:nameEl.value.trim(),node_labels:labelsEl?labelsEl.value.trim():''})});var d=await r.json();if(statEl)statEl.textContent=d&&d.name?'Registered: '+d.name:(d&&d.error||'Failed');}catch(e){if(statEl)statEl.textContent=e.message;}
      };
      // Load registered graphs list
      var graphList = detailEl.querySelector('.vg-sec-graph-list');
      if(graphList){
        fetch(apiBase+'/fabric/graphs').then(function(r){return r.json();}).then(function(d){
          graphList.innerHTML = (d.graphs||[]).map(function(g){return '<div style="padding:2px 0">'+esc(g.name)+(g.user_registered?' <span style="color:var(--acc,#5a9e8f);font-size:8px">(custom)</span>':'')+'</div>';}).join('')||'None';
        }).catch(function(){graphList.textContent='Failed to load';});
      }
      // Host callback
      if (opts.onNodeSelect) {
        try { opts.onNodeSelect(node, detailEl, instance); } catch(_){}
      }
    }

    // ════════════════════════════════════════════════════════════════════
    // BOTTOM DRAWER SYSTEM
    // ════════════════════════════════════════════════════════════════════
    // A tab rail along the bottom of the canvas. Each drawer is a named
    // panel that can hold: a terminal log, a data table, or long-form content.
    // Panels registered here appear as tabs; click opens the drawer.
    // API:
    //   instance.bottomDrawer.log(msg, type)        — append line to terminal
    //   instance.bottomDrawer.clearLog()             — clear terminal
    //   instance.bottomDrawer.showTable(cols, rows, title) — show/replace table
    //   instance.bottomDrawer.showContent(title, text, opts) — show long-form text
    //   instance.bottomDrawer.open(id)               — open named panel
    //   instance.bottomDrawer.close()                — collapse drawer
    //   instance.bottomDrawer.registerPanel(def)     — {id, title, icon, build(bodyEl)}
    (function(){
      var bdAreaEl   = container.querySelector('.vg-bottom-area');
      var bdRailEl   = container.querySelector('.vg-bd-rail');
      var bdPanelsEl = container.querySelector('.vg-bd-panels');
      if (!bdAreaEl || !bdRailEl || !bdPanelsEl) return;

      var BD_DEFAULT_H = opts.bottomDrawerHeight || 160;
      var BD_MIN_H     = 60;
      var BD_MAX_H     = 520;
      var _bdH         = BD_DEFAULT_H;
      var _activeBd    = null;
      var _collapsed   = true;
      var _panels      = {};   // id -> {tabEl, panelEl, def}

      // ── Right side of rail: resize handle + collapse button ───────────
      var _railRight = document.createElement('div');
      _railRight.className = 'vg-bd-rail-right';
      _railRight.innerHTML =
        '<span class="vg-bd-resize" title="Drag to resize">\u2195</span>' +
        '<span class="vg-bd-collapse" title="Collapse">\u25bc</span>';
      bdRailEl.appendChild(_railRight);

      var _resizeEl  = _railRight.querySelector('.vg-bd-resize');
      var _collapseEl= _railRight.querySelector('.vg-bd-collapse');

      // Drag-to-resize
      _resizeEl.addEventListener('mousedown', function(ev){
        ev.preventDefault();
        var startY = ev.clientY, startH = _bdH;
        function onMove(e){
          var dY = startY - e.clientY;   // up = bigger
          _bdH = Math.max(BD_MIN_H, Math.min(BD_MAX_H, startH + dY));
          if (!_collapsed) bdPanelsEl.style.height = _bdH + 'px';
        }
        function onUp(){ document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp); }
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
      });

      // Collapse/expand
      _collapseEl.addEventListener('click', function(){
        _collapsed = !_collapsed;
        bdPanelsEl.classList.toggle('collapsed', _collapsed);
        if (!_collapsed) bdPanelsEl.style.height = _bdH + 'px';
        _collapseEl.textContent = _collapsed ? '\u25b2' : '\u25bc';
      });

      function _setActive(id){
        if (_activeBd === id && !_collapsed) {
          // toggle off
          _collapsed = true;
          bdPanelsEl.classList.add('collapsed');
          _collapseEl.textContent = '\u25b2';
          _activeBd = null;
          Object.keys(_panels).forEach(function(k){ _panels[k].tabEl.classList.remove('on'); });
          return;
        }
        _activeBd = id;
        _collapsed = false;
        bdPanelsEl.classList.remove('collapsed');
        bdPanelsEl.style.height = _bdH + 'px';
        _collapseEl.textContent = '\u25bc';
        Object.keys(_panels).forEach(function(k){
          var p = _panels[k];
          p.tabEl.classList.toggle('on', k === id);
          p.panelEl.classList.toggle('on', k === id);
        });
        try { resize(); } catch(e){}
      }

      function _registerPanel(def){
        if (!def || !def.id) return;
        if (_panels[def.id]) return;  // already registered

        // Show the area
        bdAreaEl.style.display = '';

        // Tab
        var tab = document.createElement('div');
        tab.className = 'vg-bd-tab';
        tab.innerHTML = (def.icon ? '<span>' + def.icon + '</span>' : '') +
          '<span>' + esc(def.title || def.id) + '</span>' +
          '<span class="vg-bd-badge">0</span>';
        tab.onclick = function(){ _setActive(def.id); };
        // Insert before the right-rail control group
        bdRailEl.insertBefore(tab, _railRight);

        // Panel
        var panelEl = document.createElement('div');
        panelEl.className = 'vg-bd-panel';
        panelEl.style.height = '100%';
        bdPanelsEl.appendChild(panelEl);

        _panels[def.id] = { tabEl: tab, panelEl: panelEl, def: def };

        // Mount the content
        if (typeof def.build === 'function') {
          try { def.build(panelEl, instance); } catch(e){ panelEl.innerHTML = '<div style="color:var(--err,#c96b6b);padding:6px;font-size:9px">' + (e && e.message || e) + '</div>'; }
        }
      }

      // ── Built-in panel: Terminal ──────────────────────────────────────
      var _termEl = null, _termLines = [], _termBadge = null;
      _registerPanel({
        id: 'terminal', title: 'Terminal', icon: '\u229e',
        build: function(body, inst){
          body.style.display = 'flex'; body.style.flexDirection = 'column'; body.style.height = '100%';
          body.innerHTML =
            '<div class="vg-bd-term-bar">' +
              '<span style="color:var(--dim,#6a6058)">Discovery log</span>' +
              '<span class="vg-bd-term-count" style="color:var(--dim,#6a6058);margin-left:4px"></span>' +
              '<button onclick="this.closest(\'.vg-bd-panel\').querySelector(\'.vg-bd-term\').innerHTML=\'\';window._vgTermLines=[]" style="margin-left:auto">Clear</button>' +
            '</div>' +
            '<div class="vg-bd-term"></div>';
          _termEl = body.querySelector('.vg-bd-term');
          _termBadge = _panels['terminal'] && _panels['terminal'].tabEl.querySelector('.vg-bd-badge');
          // Re-render existing lines (if any)
          if (_termLines.length) _termEl.innerHTML = _termLines.join('');
        }
      });

      // ── Built-in panel: Table ─────────────────────────────────────────
      var _tblPanelEl = null, _tblData = null, _tblSearch = '';
      _registerPanel({
        id: 'table', title: 'Table', icon: '\u25a4',
        build: function(body, inst){
          body.style.display = 'flex'; body.style.flexDirection = 'column'; body.style.height = '100%';
          body.innerHTML =
            '<div class="vg-bd-tbl-bar">' +
              '<span class="vg-bd-tbl-title" style="color:var(--acc,#5a9e8f);font-weight:600;font-size:9px">No table loaded</span>' +
              '<input class="vg-bd-tbl-search" placeholder="filter rows\u2026" style="max-width:160px">' +
              '<span class="vg-bd-tbl-count" style="color:var(--dim,#6a6058)"></span>' +
            '</div>' +
            '<div class="vg-bd-tbl-wrap"><table class="vg-bd-tbl"><thead><tr class="vg-bd-tbl-head-row"></tr></thead><tbody class="vg-bd-tbl-body"></tbody></table></div>';
          _tblPanelEl = body;
          var si = body.querySelector('.vg-bd-tbl-search');
          si.addEventListener('input', function(){ _tblSearch = si.value; _tblRender(); });
          // Re-render if data already set
          if (_tblData) _tblRenderFull(_tblData.cols, _tblData.rows, _tblData.title);
        }
      });

      function _tblRender(){
        if (!_tblPanelEl || !_tblData) return;
        var rows = _tblData.rows;
        var q = (_tblSearch || '').toLowerCase();
        if (q) rows = rows.filter(function(r){ return r.some(function(v){ return String(v).toLowerCase().indexOf(q) >= 0; }); });
        var tbody = _tblPanelEl.querySelector('.vg-bd-tbl-body');
        var ct = _tblPanelEl.querySelector('.vg-bd-tbl-count');
        if (tbody) tbody.innerHTML = rows.map(function(r){
          return '<tr>' + r.map(function(v){
            var s = v == null ? '' : String(v);
            var isUrl = /^https?:\/\//.test(s.trim());
            var isLong = s.length > 120;
            if (isUrl) {
              return '<td title="' + esc(s.slice(0, 300)) + '"><a href="' + esc(s) + '" target="_blank" rel="noopener" style="color:var(--acc,#5a9e8f);text-decoration:none">' + esc(s.slice(0, 80)) + (s.length > 80 ? '\u2026' : '') + '</a></td>';
            }
            return '<td class="' + (isLong ? 'wrap' : '') + '" title="' + esc(s.slice(0, 300)) + '">' + esc(isLong ? s.slice(0, 300) + (s.length > 300 ? '\u2026' : '') : s) + '</td>';
          }).join('') + '</tr>';
        }).join('');
        if (ct) ct.textContent = rows.length + (q ? '/' + _tblData.rows.length : '') + ' rows';
      }
      function _tblRenderFull(cols, rows, title){
        if (!_tblPanelEl) return;
        var head = _tblPanelEl.querySelector('.vg-bd-tbl-head-row');
        var titleEl = _tblPanelEl.querySelector('.vg-bd-tbl-title');
        if (head) head.innerHTML = cols.map(function(c){ return '<th>' + esc(c) + '</th>'; }).join('');
        if (titleEl) titleEl.textContent = title || 'Table';
        _tblData = { cols: cols, rows: rows, title: title };
        _tblSearch = '';
        var si = _tblPanelEl.querySelector('.vg-bd-tbl-search'); if (si) si.value = '';
        _tblRender();
        var badge = _panels['table'] && _panels['table'].tabEl.querySelector('.vg-bd-badge');
        if (badge){ badge.textContent = rows.length; _panels['table'].tabEl.classList.toggle('has-content', rows.length > 0); }
      }

      // ── Built-in panel: Content ───────────────────────────────────────
      var _contentPanelEl = null;
      _registerPanel({
        id: 'content', title: 'Content', icon: '\u2261',
        build: function(body, inst){
          body.style.display = 'flex'; body.style.flexDirection = 'column'; body.style.height = '100%';
          body.innerHTML =
            '<div class="vg-bd-content-bar">' +
              '<span class="vg-bd-content-title">No content loaded</span>' +
              '<span class="vg-bd-content-chars" style="color:var(--dim,#6a6058);font-size:8.5px"></span>' +
              '<button style="font-size:8px;padding:1px 6px;background:var(--bg2,#272421);border:1px solid var(--border,#3a3530);color:var(--dim,#6a6058);border-radius:2px;cursor:pointer" onclick="this.closest(\'.vg-bd-panel\').querySelector(\'.vg-bd-content\').innerHTML=\'\'">Clear</button>' +
            '</div>' +
            '<div class="vg-bd-content"></div>';
          _contentPanelEl = body;
        }
      });

      // ── Public bottomDrawer API ───────────────────────────────────────
      var _logCount = 0;
      var _bottomDrawerAPI = {
        log: function(msg, type){
          // type: ok | err | warn | info | acc | dim (mirrors fdsc-log colors)
          var c = type === 'ok'   ? 'var(--ok,#6db87a)'
                : type === 'err'  ? 'var(--err,#c96b6b)'
                : type === 'warn' ? 'var(--acc3,#c9955a)'
                : type === 'acc'  ? 'var(--acc,#5a9e8f)'
                : type === 'info' ? 'var(--acc2,#8fb87a)'
                : 'var(--dim,#8a8278)';
          var t = new Date().toLocaleTimeString('en-GB', {hour12:false});
          var line = '<div style="color:' + c + ';padding:0 0 1px"><span style="color:var(--dim,#6a6058)">[' + t + ']</span> ' + esc(msg) + '</div>';
          _termLines.push(line);
          if (_termLines.length > 400) _termLines = _termLines.slice(-300);
          _logCount++;
          if (_termEl){ _termEl.insertAdjacentHTML('beforeend', line); _termEl.scrollTop = _termEl.scrollHeight; }
          if (_termBadge){ _termBadge.textContent = _logCount; _panels['terminal'].tabEl.classList.add('has-content'); }
        },
        clearLog: function(){
          _termLines = []; _logCount = 0;
          if (_termEl) _termEl.innerHTML = '';
          if (_termBadge){ _termBadge.textContent = '0'; _panels['terminal'].tabEl.classList.remove('has-content'); }
        },
        showTable: function(cols, rows, title){
          _tblRenderFull(cols, rows, title);
          _setActive('table');
        },
        showContent: function(title, text, opts2){
          if (!_contentPanelEl) return;
          var ct = _contentPanelEl.querySelector('.vg-bd-content-title');
          var ch = _contentPanelEl.querySelector('.vg-bd-content-chars');
          var body = _contentPanelEl.querySelector('.vg-bd-content');
          if (ct) ct.textContent = title || 'Content';
          if (ch) ch.textContent = text ? text.length + ' chars' : '';
          if (body){
            var html = esc(text || '');
            // Linkify URLs
            html = html.replace(/(https?:\/\/[^\s<"]+)/g, function(m){ return '<a href="' + esc(m) + '" target="_blank" rel="noopener" style="color:var(--acc,#5a9e8f)">' + esc(m) + '</a>'; });
            body.innerHTML = html;
          }
          var badge = _panels['content'] && _panels['content'].tabEl.querySelector('.vg-bd-badge');
          if (badge){ badge.textContent = text ? '\u2713' : ''; _panels['content'].tabEl.classList.toggle('has-content', !!text); }
          _setActive('content');
        },
        open: function(id){ _setActive(id || 'terminal'); },
        close: function(){
          _collapsed = true;
          bdPanelsEl.classList.add('collapsed');
          _collapseEl.textContent = '\u25b2';
          _activeBd = null;
          Object.keys(_panels).forEach(function(k){ _panels[k].tabEl.classList.remove('on'); });
        },
        isOpen: function(id){ return !_collapsed && _activeBd === (id || _activeBd); },
        registerPanel: function(def){ _registerPanel(def); },
      };

      // Auto-open terminal on first log if opts.autoOpenTerminal is true
      if (opts.autoOpenTerminal) {
        var _origLog2 = _bottomDrawerAPI.log.bind(_bottomDrawerAPI);
        var _autoOpened = false;
        _bottomDrawerAPI.log = function(msg, type){
          if (!_autoOpened && !_activeBd){ _autoOpened = true; _setActive('terminal'); }
          _origLog2(msg, type);
        };
      }

      // Expose via _bdAPI so it can be wired to instance after instance is created
      container._vgBdApi = _bottomDrawerAPI;
    })();

    // ── Public instance API ──────────────────────────────────────────────
    var instance = {
      state:          state,
      load:           load,
      addNode:        addNode,
      addEdge:        addEdge,
      expandEntities: expandNode,
      expandNode:     expandNode,
      collapseNode:   collapseNode,
      expandRecords:  expandRecords,
      fetchSnapshot:  fetchSnapshot,
      showDetail:     showDetail,
      hideDetail:     hideDetail,
      runAction:      function(actionId, node){
        var label = (node && (node.type || (node.labels && node.labels[0]))) || 'Node';
        var actions = state.actionsCache[label];
        if (!actions) return null;
        var a = actions.find(function(x){return x.id === actionId;});
        if (!a) return null;
        return runAction(a, actions.indexOf(a), node);
      },
      pulseNode:      function(id){
        var n = state.nodeIndex[id];
        if (n) n._pulseUntil = Date.now() + 1500;
      },
      wake:           wake,
      fit:            function(){ if (fitBtn) try { fitBtn.onclick(); } catch(e){} },
      // ── Latent map ───────────────────────────────────────────────────────
      // The WorldView system calls this to render its latent space statically.
      // positions: { nodeId: {x, y}, ... } in any coordinate space (we fit-to-view).
      // Switches the graph into the static 'latent-map' layout (frozen physics).
      setLatentMap: function(positions){
        state._latentMap = positions || {};
        _applyLayout('latent-map');
        // reflect the active layout chip if present
        container.querySelectorAll('.vg-view-chip').forEach(function(vc){
          vc.classList.toggle('on', vc.dataset.layout === 'latent-map');
        });
      },
      clearLatentMap: function(){ state._latentMap = null; _applyLayout('default'); },
      // ── Layer / group controls ──────────────────────────────────────────
      // getLayers() -> { name: {visible, physics, nodeCount, edgeCount} }
      getLayers: function(){
        var out = {};
        Object.keys(state.layers || {}).forEach(function(k){
          out[k] = { visible: state.layers[k].visible !== false,
                     physics: state.layers[k].physics !== false,
                     nodeCount: 0, edgeCount: 0 };
        });
        state.nodes.forEach(function(n){ if (out[n._layer]) out[n._layer].nodeCount++; });
        state.edges.forEach(function(e){ if (out[e._layer]) out[e._layer].edgeCount++; });
        return out;
      },
      setLayerVisible: function(name, on){
        _ensureLayer(name).visible = on !== false;
        _applyVis(); _rebuildChips && _rebuildChips(); draw();
        _renderLayerUI();
      },
      setLayerPhysics: function(name, on){
        _ensureLayer(name).physics = on !== false;
        wake();                       // re-energise so the change takes effect
        _renderLayerUI();
      },
      resize:         function(){ try { resize(); } catch(e){} wake(); },
      stop:           stopTick,
      destroy:        function(){
        stopTick();
        _liveUnsubs.forEach(function(u){ try{u();}catch(_){} });
        container.innerHTML = '';
      },
      focusNode:      function(id){
        var n = state.nodeIndex[id];
        if (n) {
          state.off.x = W / 2 - n.x * state.scale;
          state.off.y = H / 2 - n.y * state.scale;
          state.selected = n;
          showDetail(n);
        }
      },
      search:         function(q){ if (searchEl) { searchEl.value = q; searchEl.dispatchEvent(new Event('input')); } },
      clear:          function(){ load({ nodes: [], edges: [] }); },
      setLayer:       function(l){ if (layerEl) { layerEl.value = l; layerEl.dispatchEvent(new Event('change')); } },
      getNode:        function(id){ return state.nodeIndex[id]; },
      colorFor:       nodeColor,
      container:      container,
      canvas:         canvas,
      eventBus:       opts.eventBus || _getSharedBus(),
      fetchMemory:    fetchMemory,
      applyLayout:    _applyLayout,
      rebuildChips:   _rebuildChips,
      applyVis:       _applyVis,
      updateDebug:    _updateDebug,
      showDetail:     showDetail,
      getDetailEl:    function(){ return detailEl; },
      bottomDrawer:   null,  // populated below from container._vgBdApi
    };

    // Wire bottom drawer API (set up by the IIFE above)
    if (container._vgBdApi) {
      instance.bottomDrawer = container._vgBdApi;
      delete container._vgBdApi;
    } else {
      // Fallback no-op API so callers never need to null-check
      instance.bottomDrawer = { log: function(){}, clearLog: function(){}, showTable: function(){}, showContent: function(){}, open: function(){}, close: function(){}, isOpen: function(){ return false; }, registerPanel: function(){} };
    }

    // ── Modular sidebar host ──────────────────────────────────────────────
    // Build a left-side icon rail + panel area and mount any registered
    // companion panels. Opt-out with opts.sidebar === false; restrict the set
    // with opts.sidebarPanels = ['loom','discover',...] (default: all registered).
    var _sidebarEl = null, _sbRailEl = null, _sbPanelsEl = null;
    var _mountedPanels = {};   // id -> { def, tabEl, panelEl, bodyEl, mounted }
    var _activePanelId = null;

    function _ensureSidebar(){
      if (_sidebarEl || opts.sidebar === false) return _sidebarEl;
      // The container holds [vg-left][vg-canvas-area] laid out as a flex row.
      // Some host embeds don't set display:flex on the container itself (they
      // rely on outer CSS), in which case an inserted sidebar would collapse to
      // zero size and never show. Force the row layout so the rail is always
      // visible regardless of host styling.
      var cs = (window.getComputedStyle ? window.getComputedStyle(container) : null);
      if (!cs || cs.display !== 'flex') { container.style.display = 'flex'; }
      if (!cs || (cs.flexDirection !== 'row' && cs.flexDirection !== 'row-reverse')) {
        container.style.flexDirection = 'row';
      }
      // Guarantee the container can actually show height. Many embeds give the
      // container an explicit height; if it has none, fall back to 100%.
      if (container.style.height === '' && (!cs || (cs.height === 'auto' || cs.height === '0px'))) {
        container.style.height = container.style.height || '100%';
      }
      _sidebarEl = document.createElement('div');
      _sidebarEl.className = 'vg-sidebar collapsed';
      _sbRailEl = document.createElement('div');
      _sbRailEl.className = 'vg-sb-rail';
      _sbPanelsEl = document.createElement('div');
      _sbPanelsEl.className = 'vg-sb-panels';
      _sidebarEl.appendChild(_sbRailEl);
      _sidebarEl.appendChild(_sbPanelsEl);
      // Insert as the first child so the sidebar sits on the far left, before
      // the existing vg-left filter panel and the canvas area.
      container.insertBefore(_sidebarEl, container.firstChild);

      // ── Unify the two left menus ─────────────────────────────────────────
      // Absorb the graph-controls panel (.vg-left) INTO the sidebar as its first
      // tab, so there's a single left-hand menu: one icon rail with
      // [Controls, Loom, WorldView, …] feeding one shared panel area, instead of
      // two competing left panels.
      var _leftEl = container.querySelector('.vg-left');
      if (_leftEl && _leftEl.parentNode !== _sbPanelsEl) {
        // strip the standalone-panel chrome that no longer applies inside a tab
        _leftEl.classList.remove('collapsed');
        _leftEl.style.width = '';
        _leftEl.style.minWidth = '';
        _leftEl.style.borderRight = 'none';
        _leftEl.style.flex = '1';
        _leftEl.style.height = '100%';
        _leftEl.style.overflow = 'auto';
        // Hide the old standalone collapse toggle — it doesn't apply inside a tab
        var _oldToggle = _leftEl.querySelector('.vg-left-toggle');
        if (_oldToggle) _oldToggle.style.display = 'none';
        // Hide the old header since the sidebar panel has its own header
        var _oldHd = _leftEl.querySelector('.vg-left-hd');
        if (_oldHd) _oldHd.style.display = 'none';
        // wrap it as a sidebar panel
        var cPanel = document.createElement('div');
        cPanel.className = 'vg-sb-panel';
        cPanel.setAttribute('data-panel', 'controls');
        cPanel.appendChild(_leftEl);
        _sbPanelsEl.appendChild(cPanel);
        // rail tab for controls (first, and active by default)
        var cTab = document.createElement('div');
        cTab.className = 'vg-sb-tab';
        cTab.setAttribute('data-panel', 'controls');
        cTab.innerHTML = '\u2699<span class="vg-sb-tip">Controls</span>';
        cTab.onclick = function(){ _activatePanel('controls'); };
        _sbRailEl.appendChild(cTab);
        _mountedPanels['controls'] = { def: { id:'controls', title:'Controls', order:0 },
                                        tabEl: cTab, panelEl: cPanel,
                                        bodyEl: _leftEl, mounted: true };
        // open Controls by default so the graph filters are visible on load
        setTimeout(function(){ try { _activatePanel('controls'); } catch(e){} }, 0);
      }
      return _sidebarEl;
    }

    function _activatePanel(id){
      // Toggle off if clicking the already-active tab → collapse the sidebar.
      if (_activePanelId === id) {
        _activePanelId = null;
        _sidebarEl.classList.add('collapsed');
        Object.keys(_mountedPanels).forEach(function(k){
          _mountedPanels[k].tabEl.classList.remove('on');
          _mountedPanels[k].panelEl.classList.remove('on');
        });
        // Canvas width changed — let the layout re-measure.
        try { resize(); } catch(e){}
        return;
      }
      _activePanelId = id;
      _sidebarEl.classList.remove('collapsed');
      Object.keys(_mountedPanels).forEach(function(k){
        var mp = _mountedPanels[k];
        var on = (k === id);
        mp.tabEl.classList.toggle('on', on);
        mp.panelEl.classList.toggle('on', on);
        // Lazy-mount: only call the panel's mount() the first time it's shown.
        if (on && !mp.mounted) {
          mp.mounted = true;
          try {
            mp.def.mount(mp.bodyEl, instance, {
              activate:        function(){ _activatePanel(id); },
              isActive:        function(){ return _activePanelId === id; },
              graphContainer:  container,
              apiBase:         apiBase,
              eventBus:        instance.eventBus,
            });
          } catch(e){
            if (typeof console !== 'undefined') console.warn('panel mount', id, e);
            mp.bodyEl.innerHTML = '<div style="color:var(--err,#c96b6b);font-size:9px;padding:8px">Panel failed to load: ' + (e && e.message || e) + '</div>';
          }
        }
      });
      try { resize(); } catch(e){}
    }

    function _attachSidebarPanel(def){
      if (opts.sidebar === false) return;
      // Respect an explicit whitelist if provided
      if (opts.sidebarPanels && opts.sidebarPanels.indexOf(def.id) === -1) return;
      if (_mountedPanels[def.id]) return;  // already attached
      _ensureSidebar();

      // Rail tab
      var tab = document.createElement('div');
      tab.className = 'vg-sb-tab';
      tab.setAttribute('data-panel', def.id);
      tab.innerHTML = (def.icon || '\u25a3') +
        '<span class="vg-sb-tip">' + (def.title || def.id) + '</span>';
      tab.onclick = function(){ _activatePanel(def.id); };

      // Panel container + header + body
      var panel = document.createElement('div');
      panel.className = 'vg-sb-panel';
      panel.setAttribute('data-panel', def.id);
      var hd = document.createElement('div');
      hd.className = 'vg-sb-panel-hd';
      hd.innerHTML = '<span>' + (def.title || def.id) + '</span>' +
        '<span class="vg-sb-close" style="cursor:pointer;font-size:13px;line-height:1" title="Close">\u00d7</span>';
      var body = document.createElement('div');
      body.className = 'vg-sb-panel-body';
      panel.appendChild(hd);
      panel.appendChild(body);
      hd.querySelector('.vg-sb-close').onclick = function(){ _activatePanel(def.id); };

      // Insert in order
      var order = def.order || 100;
      var railTabs = Array.prototype.slice.call(_sbRailEl.children);
      var inserted = false;
      for (var i = 0; i < railTabs.length; i++) {
        var otherId = railTabs[i].getAttribute('data-panel');
        var otherDef = _mountedPanels[otherId] && _mountedPanels[otherId].def;
        if (otherDef && (otherDef.order || 100) > order) {
          _sbRailEl.insertBefore(tab, railTabs[i]);
          _sbPanelsEl.insertBefore(panel, _mountedPanels[otherId].panelEl);
          inserted = true;
          break;
        }
      }
      if (!inserted) {
        _sbRailEl.appendChild(tab);
        _sbPanelsEl.appendChild(panel);
      }

      _mountedPanels[def.id] = { def: def, tabEl: tab, panelEl: panel,
                                  bodyEl: body, mounted: false };
    }

    // Expose the attach hook so registerPanel() can reach existing graphs,
    // and a couple of helpers for host code / panels.
    instance._attachSidebarPanel = _attachSidebarPanel;
    instance.openPanel  = function(id){ if (_mountedPanels[id]) _activatePanel(id); };
    instance.closePanel = function(){ if (_activePanelId) _activatePanel(_activePanelId); };
    instance.hasPanel   = function(id){ return !!_mountedPanels[id]; };

    // Attach all currently-registered panels to this fresh graph.
    _PANEL_REGISTRY.forEach(function(def){ _attachSidebarPanel(def); });
    _LIVE_GRAPHS.push(instance);

    // Auto-open a default sidebar panel if specified
    if (opts.defaultPanel) {
      // Defer so all companion scripts that register panels have run
      setTimeout(function(){
        if (_mountedPanels[opts.defaultPanel]) _activatePanel(opts.defaultPanel);
      }, 0);
    }

    // Clean up registration on destroy
    var _origDestroy = instance.destroy;
    instance.destroy = function(){
      var ix = _LIVE_GRAPHS.indexOf(instance);
      if (ix >= 0) _LIVE_GRAPHS.splice(ix, 1);
      Object.keys(_mountedPanels).forEach(function(k){
        var mp = _mountedPanels[k];
        if (mp.mounted && typeof mp.def.unmount === 'function') {
          try { mp.def.unmount(mp.bodyEl, instance); } catch(e){}
        }
      });
      if (_origDestroy) _origDestroy();
    };

    container._veraGraph = instance;
    startTick();
    return instance;
  }

  // ─── Register on veraUI ──────────────────────────────────────────────────
  if (typeof window !== 'undefined') {
    window.veraUI = window.veraUI || {};
    window.veraUI.Graph = {
      create:        createGraph,
      colors:        COL,
      nodeColor:     nodeColor,
      edgeColor:     edgeColor,
      eventBus:      _getSharedBus,
      // Modular sidebar plugin API — companion files call registerPanel().
      registerPanel: registerPanel,
      listPanels:    listPanels,
    };
  }
})();