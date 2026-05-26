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
      '.vg-left{width:220px;flex-shrink:0;background:var(--bg1,#1f1d1a);border-right:1px solid var(--border,#3a3530);display:flex;flex-direction:column;overflow:hidden;transition:width .2s ease,min-width .2s ease}',
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
      nodeIndex: {},
      currentLayer: opts.defaultLayer || 'fabric',
      currentParams: opts.layerOpts || {},
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
          '<input class="vg-search" placeholder="Search nodes..." style="flex:1;min-width:120px;font-size:10px;padding:3px 6px;background:var(--bg2);color:var(--text);border:1px solid var(--border);border-radius:3px">' : '') +
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
    if (showLeft) {
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
          '<div class="vg-sl">Node Types <span class="vg-type-cnt" style="font-size:7.5px"></span></div>' +
          '<div class="vg-chips vg-type-chips"></div>' +
          '<div class="vg-sl">Edge Types <span class="vg-edge-cnt" style="font-size:7.5px"></span></div>' +
          '<div class="vg-chips vg-edge-chips"></div>' +
          // View mode
          '<div class="vg-sl">View Mode</div>' +
          '<div style="display:flex;gap:2px;flex-wrap:wrap;margin-bottom:6px">' +
            '<span class="vg-view-chip on" data-layout="default">Default</span>' +
            '<span class="vg-view-chip" data-layout="force-axis">Force+Axis</span>' +
            '<span class="vg-view-chip" data-layout="timeline">Timeline</span>' +
            '<span class="vg-view-chip" data-layout="hierarchy">Hierarchy</span>' +
            '<span class="vg-view-chip" data-layout="radial">Radial</span>' +
          '</div>' +
          // Per-layout controls
          '<div class="vg-layout-ctrls">' +
            '<div class="vg-lc-force-axis" style="display:none">' +
              '<div class="vg-ctrl"><label>X axis</label><select class="vg-ax-x"><option value="time">Time</option><option value="importance">Importance</option><option value="source">Source</option><option value="category">Category</option></select></div>' +
              '<div class="vg-ctrl"><label>Y axis</label><select class="vg-ax-y"><option value="type">Type</option><option value="importance">Importance</option><option value="session">Session</option></select></div>' +
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
          '<div class="vg-sl">Search</div>' +
          '<input type="search" class="vg-search vg-node-search" placeholder="Filter nodes...">' +
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
      '<div class="vg-canvas-area">' + headerHTML +
      '<div class="vg-canvas-wrap" style="position:relative;' + wrapHeightCss + '">' +
        '<canvas class="vg-canvas" style="width:100%;height:' + canvasHeightCss + ';background:var(--bg0);border:1px solid var(--border);border-radius:var(--radius,4px);cursor:grab;display:block"></canvas>' +
        '<div class="vg-tooltip" style="position:absolute;display:none;background:var(--bg2);border:1px solid var(--border);border-radius:3px;padding:4px 8px;font-size:9.5px;color:var(--text);pointer-events:none;z-index:5;max-width:220px;line-height:1.4"></div>' +
        '<div class="vg-detail" style="position:absolute;top:0;right:0;width:320px;height:100%;background:var(--bg1);border-left:1px solid var(--border);overflow-y:auto;display:none;font-size:10.5px"></div>' +
        '<div class="vg-actionbar" style="position:absolute;left:0;bottom:0;right:0;display:none;background:var(--bg1);border-top:1px solid var(--border);max-height:42%;overflow-y:auto;font-size:10px"></div>' +
      '</div>' +
      (opts.showLegend !== false ?
        '<div class="vg-legend"></div>' : '') +
      '</div>'; // close vg-canvas-area

    var canvas    = container.querySelector('.vg-canvas');
    var tooltip   = container.querySelector('.vg-tooltip');
    var detailEl  = container.querySelector('.vg-detail');
    var actionEl  = container.querySelector('.vg-actionbar');
    var metaEl    = container.querySelector('.vg-meta');
    var legendEl  = container.querySelector('.vg-legend');
    var searchEl  = container.querySelector('.vg-node-search') || container.querySelector('.vg-search');
    var layerEl   = container.querySelector('.vg-layer');
    var relayoutBtn = container.querySelector('.vg-relayout');
    var fitBtn    = container.querySelector('.vg-fit');

    var ctx = canvas.getContext('2d');
    var W, H, DPR;

    function resize(){
      var rect = canvas.getBoundingClientRect();
      W = rect.width || canvas.offsetWidth || 640;
      H = rect.height || canvas.offsetHeight || DEFAULT_HEIGHT;
      DPR = window.devicePixelRatio || 1;
      canvas.width  = W * DPR;
      canvas.height = H * DPR;
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    }
    resize();
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(function(){ resize(); state.frozen = false; state.tickCount = 0; }).observe(canvas);
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
        if (state._edgeOff && state._edgeOff.size && state._edgeOff.has(e.rel||'RELATED')) return;
        var isSel = state.selected && (state.selected.id === a.id || state.selected.id === b.id);
        // Apply edge style function if provided
        var es = opts.edgeStyleFn ? opts.edgeStyleFn(e) : null;
        var ec = (es && es.color) || edgeColor(e.rel);
        var lw = isSel ? 1.8 : ((es && es.width) || 1);
        ctx.beginPath();
        if (es && es.dash) ctx.setLineDash(es.dash);
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
        ctx.fillStyle = col + ((hov || sel || hl) ? 'ff' : 'aa');
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
      if (state.tickCount > 280 && !state.drag) state.frozen = true;
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

      state.nodes.forEach(function(a){
        if (state.drag === a) { a.vx = 0; a.vy = 0; return; }
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
        var dx = b.x - a.x, dy = b.y - a.y;
        var d  = Math.sqrt(dx * dx + dy * dy) + 0.1;
        // Per-edge spring from edgeStyleFn
        var es = opts.edgeStyleFn ? opts.edgeStyleFn(e) : null;
        var restLen = (es && es.springLength) || 80;
        var str     = (es && es.springStrength) || 0.012;
        var f  = str * (d - restLen);
        if (state.drag !== a) { a.vx += f * dx / d; a.vy += f * dy / d; }
        if (state.drag !== b) { b.vx -= f * dx / d; b.vy -= f * dy / d; }
      });
      state.nodes.forEach(function(n){
        if (state.drag === n) return;
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
    function wake(){ state.frozen = false; state.tickCount = 0; }
    function startTick(){
      if (state.animHandle) {
        try { cancelAnimationFrame(state.animHandle); } catch(e) {}
        try { clearTimeout(state.animHandle); } catch(e) {}
      }
      state.stopped = false;
      tick();
    }
    function stopTick(){
      state.stopped = true;
      if (state.animHandle) {
        try { cancelAnimationFrame(state.animHandle); } catch(e) {}
        try { clearTimeout(state.animHandle); } catch(e) {}
      }
      state.animHandle = null;
    }

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

    // ── Search ────────────────────────────────────────────────────────────
    if (searchEl) {
      searchEl.addEventListener('input', throttle(function(){
        var q = (searchEl.value || '').trim().toLowerCase();
        state.searchHighlight = new Set();
        if (q) {
          state.nodes.forEach(function(n){
            var l = String(n.label || '').toLowerCase();
            var i = String(n.id || '').toLowerCase();
            var name = (n.props && n.props.name) ? String(n.props.name).toLowerCase() : '';
            if (l.indexOf(q) >= 0 || i.indexOf(q) >= 0 || name.indexOf(q) >= 0) {
              state.searchHighlight.add(n.id);
            }
          });
          var firstId = state.searchHighlight.values().next().value;
          if (firstId) {
            var node = state.nodes.find(function(n){return n.id === firstId;});
            if (node) {
              state.off.x = W / 2 - node.x * state.scale;
              state.off.y = H / 2 - node.y * state.scale;
            }
          }
        }
      }, 150));
    }

    if (relayoutBtn) relayoutBtn.onclick = wake;
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
    var _typeOff = new Set(), _edgeOff = new Set();
    var _layoutMode = 'default';
    var _sessions = [], _selectedSids = new Set();

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

    // ── Filter chip rebuild ─────────────────────────────────────────────
    function _rebuildChips(){
      if (!_typeChipEl || !_edgeChipEl) return;
      var tc={}, ec={};
      state.nodes.forEach(function(n){ var t=n.type||'?'; tc[t]=(tc[t]||0)+1; });
      state.edges.forEach(function(e){ var r=e.rel||'RELATED'; ec[r]=(ec[r]||0)+1; });
      _typeChipEl.innerHTML = Object.keys(tc).sort().map(function(t){
        return '<span class="vg-chip'+ (!_typeOff.has(t)?' on':'') +'" data-t="'+esc(t)+'">'+esc(t)+'<span class="cc">'+tc[t]+'</span></span>';
      }).join('');
      _edgeChipEl.innerHTML = Object.keys(ec).sort().map(function(r){
        return '<span class="vg-chip'+(!_edgeOff.has(r)?' on':'')+'" data-e="'+esc(r)+'">'+esc(r)+'<span class="cc">'+ec[r]+'</span></span>';
      }).join('');
      if (_typeCntEl) _typeCntEl.textContent = Object.keys(tc).length ? '('+Object.keys(tc).length+')' : '';
      if (_edgeCntEl) _edgeCntEl.textContent = Object.keys(ec).length ? '('+Object.keys(ec).length+')' : '';
    }
    if (_typeChipEl) _typeChipEl.addEventListener('click', function(ev){
      var c=ev.target.closest('.vg-chip'); if(!c) return;
      var t=c.dataset.t; if (_typeOff.has(t)) _typeOff.delete(t); else _typeOff.add(t);
      c.classList.toggle('on',!_typeOff.has(t)); _applyVis();
    });
    if (_edgeChipEl) _edgeChipEl.addEventListener('click', function(ev){
      var c=ev.target.closest('.vg-chip'); if(!c) return;
      var r=c.dataset.e; if (_edgeOff.has(r)) _edgeOff.delete(r); else _edgeOff.add(r);
      c.classList.toggle('on',!_edgeOff.has(r)); _applyVis();
    });

    function _applyVis(){
      state.nodes.forEach(function(n){ n._hidden = _typeOff.size > 0 && _typeOff.has(n.type||'?'); });
      // Edge filter stored for draw loop
      state._edgeOff = _edgeOff;
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
        state.frozen = false; wake(); return;
      }

      // Time calibration
      var tMin=Infinity,tMax=-Infinity;
      vis.forEach(function(n){
        var ts=n.props&&(n.props.created_at||n.props.timestamp);
        if(ts){var t=new Date(ts).getTime();if(Number.isFinite(t)){if(t<tMin)tMin=t;if(t>tMax)tMax=t;}}
      });
      var tRange=Math.max(tMax-tMin,1);
      function _axVal(n,ax){
        var p=n.props||{};
        if(ax==='time'){var ts=p.created_at||p.timestamp;if(!ts)return 6;var t=new Date(ts).getTime();return Math.sqrt(Math.max(0,(t-tMin)/tRange))*14;}
        if(ax==='importance')return parseFloat(p.importance||0.5)*12;
        if(ax==='source'){var sm={human:0,ai:2,tool:4,system:6,sensor:8,document:10};return sm[p.source_type]!==undefined?sm[p.source_type]:5;}
        if(ax==='category'){var h=0;var s=p.category||'';for(var i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))&0xffff;return h%13;}
        if(ax==='type'){var tm={session:0,Session:0,message:2,Memory:3,event:4,observation:5,Dataset:6,Source:7,dag:8,Entity:9,FabricRecord:10,fact:11,summary:12};return tm[n.type]!==undefined?tm[n.type]:6;}
        if(ax==='session'){var h2=0;var s2=p.session_id||n.id||'';for(var i2=0;i2<s2.length;i2++)h2=(h2*31+s2.charCodeAt(i2))&0xffff;return h2%13;}
        return 0;
      }

      if (mode === 'force-axis') {
        var axX = (container.querySelector('.vg-ax-x')||{}).value || 'time';
        var axY = (container.querySelector('.vg-ax-y')||{}).value || 'type';
        var sp = parseFloat((container.querySelector('.vg-spread')||{}).value || 200);
        vis.forEach(function(n){
          n.x = _axVal(n,axX)*sp + (Math.random()-0.5)*24;
          n.y = _axVal(n,axY)*sp*0.65 + (Math.random()-0.5)*24;
          n.vx=0; n.vy=0;
        });
        state.frozen = false; wake();

      } else if (mode === 'timeline') {
        var laneH = parseFloat((container.querySelector('.vg-tl-lane')||{}).value || 80);
        var pxH = parseFloat((container.querySelector('.vg-tl-scale')||{}).value || 60);
        var typeOrder = ['Session','session','message','Memory','event','observation','Dataset','Source','Entity','dag','FabricRecord','fact','summary'];
        vis.forEach(function(n){
          var ts=n.props&&(n.props.created_at||n.props.timestamp);
          var t=ts?new Date(ts).getTime():(tMin+tMax)/2;
          n.x = ((t-tMin)/3600000)*pxH;
          var lane=typeOrder.indexOf(n.type); if(lane<0)lane=typeOrder.length;
          n.y = lane*laneH;
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
          '<button class="vg-detail-close" style="background:none;border:none;color:var(--dim);font-size:14px;cursor:pointer;padding:0 4px">×</button>' +
        '</div>' +
        '<div style="padding:8px">' +
          '<div style="font-size:8.5px;color:var(--dim2);font-family:var(--mono,monospace);margin-bottom:6px;word-break:break-all">' + esc(node.id) + '</div>' +
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
          // Also fetch and add memory nodes as connected context
          try{
            var memRes = await fetch(apiBase+'/memory/agent/context',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
              session_id:sid, query:query, limit:5
            })});
            var memData = await memRes.json();
            // Parse context string for node-like entries and add any that have IDs
            if(memData.context && typeof memData.context === 'string'){
              ctxResult.textContent += '\n\n--- Agent Memory Context ---\n' + memData.context.slice(0,400);
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
        } else {
          line.style.color = 'var(--err)';
          line.textContent = '▸ failed: ' + (result && result.error || 'unknown');
          streamBox.appendChild(line);
        }
        streamBox.scrollTop = streamBox.scrollHeight;
      }
      if (unsub) try { unsub(); } catch(_){}

      // Re-fetch the snapshot so persisted side-effects appear
      // Only re-fetch if we're on a layer that can actually show
      // the results (not memory, unless the action produced memory data)
      setTimeout(function(){
        if (state.currentLayer && state.currentLayer !== 'memory' && instance.fetchSnapshot) {
          instance.fetchSnapshot(state.currentLayer, state.currentParams || {});
        }
      }, 600);

      if (opts.onActionDone) opts.onActionDone(action, node, result, instance);
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

    // ── load / addNode / addEdge ─────────────────────────────────────────
    function load(data){
      data = data || {};
      state.nodes = []; state.edges = []; state.nodeIndex = {};
      state.expanded = {}; state.searchHighlight = new Set();
      state.tickCount = 0; state.frozen = false;
      (data.nodes || []).forEach(function(n){ addNode(n, true); });
      (data.edges || []).forEach(function(e){ addEdge(e, true); });
      _applyVis();
      updateMeta(); updateLegend(); _rebuildChips(); _rebuildTags(); _updateDebug(); wake();
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
      var n = {
        id:    nodeSpec.id,
        label: String(lbl).slice(0, 50),
        type:  nodeSpec.type || (nodeSpec.labels && nodeSpec.labels[0]) || 'Node',
        props: nodeSpec.props || {},
        x: nodeSpec.x !== undefined ? nodeSpec.x : (W / 2 + (Math.random() - 0.5) * 200),
        y: nodeSpec.y !== undefined ? nodeSpec.y : (H / 2 + (Math.random() - 0.5) * 200),
        vx: 0, vy: 0,
        r: nodeSpec.r || (nodeSpec.type === 'Entity' ? 8 : nodeSpec.type === 'Dataset' ? 14 : 10),
      };
      if (nodeSpec._fromId && state.nodeIndex[nodeSpec._fromId]) {
        var src = state.nodeIndex[nodeSpec._fromId];
        n.x = src.x + (Math.random() - 0.5) * 120;
        n.y = src.y + (Math.random() - 0.5) * 120;
      }
      state.nodes.push(n);
      state.nodeIndex[nodeSpec.id] = n;
      if (!silent) { wake(); updateMeta(); updateLegend(); }
      return n;
    }

    function addEdge(edgeSpec, silent){
      if (!edgeSpec || !edgeSpec.from || !edgeSpec.to) return;
      for (var i = 0; i < state.edges.length; i++) {
        var e = state.edges[i];
        if (e.from === edgeSpec.from && e.to === edgeSpec.to && e.rel === edgeSpec.rel) return;
      }
      state.edges.push({
        from: edgeSpec.from, to: edgeSpec.to,
        rel:  (edgeSpec.rel || edgeSpec.label || '').slice(0, 20),
        props: edgeSpec.props || {},
      });
      if (!silent) { wake(); updateMeta(); }
    }

    function updateMeta(){
      if (!metaEl) return;
      var byType = {};
      state.nodes.forEach(function(n){
        var t = n.type || '?';
        byType[t] = (byType[t] || 0) + 1;
      });
      var summary = Object.keys(byType).map(function(t){
        return byType[t] + ' ' + t;
      }).join(', ');
      metaEl.textContent = state.nodes.length + ' nodes (' + summary + '), ' + state.edges.length + ' edges';
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
        var args={};par.querySelectorAll('[data-cp]').forEach(function(el){if(el.value)args[el.dataset.cp]=el.value;});
        try{var r=await fetch(apiBase+'/mcp/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:sel.value,arguments:args})});var d=await r.json();var c=d.content||d;if(resEl){resEl.textContent=(typeof c==='string'?c:JSON.stringify(c,null,2)).slice(0,600);resEl.style.color='var(--ok,#6db87a)';}}catch(e){if(resEl){resEl.textContent='Error: '+e.message;resEl.style.color='var(--err,#c96b6b)';}}
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
      updateDebug:    _updateDebug,
      showDetail:     showDetail,
      getDetailEl:    function(){ return detailEl; },
    };

    container._veraGraph = instance;
    startTick();
    return instance;
  }

  // ─── Register on veraUI ──────────────────────────────────────────────────
  if (typeof window !== 'undefined') {
    window.veraUI = window.veraUI || {};
    window.veraUI.Graph = {
      create:    createGraph,
      colors:    COL,
      nodeColor: nodeColor,
      edgeColor: edgeColor,
      eventBus:  _getSharedBus,
    };
  }
})();