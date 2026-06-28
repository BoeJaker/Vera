/* ============================================================================
 * <vera-flow-builder>  —  Reusable visual flow / DAG builder
 * ============================================================================
 *
 * A self-contained custom element that provides the DAG-Workshop-grade visual
 * flow builder — searchable/draggable palette → auto-laid-out SVG canvas with
 * bezier edges and inferred data-wires → schema-driven inspector — lifted into
 * a registered injectable element so any panel can build flows without
 * duplicating the implementation.
 *
 * It is DOMAIN-AGNOSTIC. The host supplies a small `provider` object that
 * adapts the builder to a domain (capabilities/DAG, dream pipelines, fabric
 * query, research, …). The element owns a generic graph model and all of the
 * palette / canvas / inspector UI; the provider supplies the palette, the node
 * schema, and (de)serialization to the host's own document format.
 *
 * USAGE
 * ─────
 *   <script src="/ui/elements/flow_builder.js"><\/script>
 *   <vera-flow-builder id="fb"></vera-flow-builder>
 *   <script>
 *     const el = document.getElementById('fb');
 *     el.setProvider(myProvider);          // see PROVIDER INTERFACE below
 *     el.loadFromSource(existingDoc);      // optional: hydrate from host doc
 *     el.addEventListener('flow:change', e => save(el.serialize()));
 *   <\/script>
 *
 * PUBLIC API
 * ──────────
 *   el.setProvider(obj)        — set the domain adapter (triggers palette load)
 *   el.getGraph()              — the generic graph model {nodes, meta}
 *   el.setGraph(graph)         — replace the model and re-render
 *   el.serialize()             — provider.serialize(graph) → host document
 *   el.loadFromSource(doc)     — provider.deserialize(doc) → model, then render
 *   el.reset()                 — clear nodes + selection
 *   el.refreshPalette()        — re-call provider.loadPalette()
 *   el.getSelected()           — currently-selected node or null
 *   el.autoLayout()            — drop all manual positions (back to auto)
 *
 * PROVIDER INTERFACE (host supplies; * = required)
 * ────────────────────────────────────────────────
 *   id                                  — short provider name
 * * loadPalette() -> [{group, items}]   — items: {type, label?, description?,
 *                                          schema?, long_running?, badges?}
 *   schemaFor(typeOrNode) -> schema     — else item.schema is used / cached
 *   createNode(item, ctx) -> node       — else a node is built from the schema
 *   validateNode(node, schema) -> [str] — else required-param check
 *   renderInspector(node, ctx)          — else the built-in schema inspector.
 *                                          May return html string, or
 *                                          {html, bind(rootEl)} to wire events.
 *   globalSection(graph, ctx) -> html   — shown in the inspector when nothing
 *                                          is selected (e.g. DAG initial state)
 * * serialize(graph) -> doc             — model → host document
 * * deserialize(doc) -> graph           — host document → model
 *   nodeCard(node, schema) -> {title,sub,line}  — node card text override
 *   onChange(graph)                     — called after any edit
 *
 *   `ctx` passed to hooks: {el, graph, schema, esc, update(), select(id)}
 *
 * SCHEMA shape (mirrors Vera cap descriptors)
 * ───────────────────────────────────────────
 *   {name, description, long_running,
 *    params:[{name,type,required,enum,default,description,properties,items}],
 *    outputs:[{name,description}], output_keys:[…], streams:[…]}
 *
 * GENERIC MODEL
 * ─────────────
 *   node  = {id, type, out, params:{name:{source:'value'|'state', value}},
 *            output_map, condition, parallel_with, pos:{x,y}|null, meta:{}}
 *   graph = {nodes:[], meta:{}}
 *
 * EVENTS (CustomEvents, bubbles:true)
 * ───────────────────────────────────
 *   flow:change   {graph}          — model changed (add/edit/remove/move)
 *   flow:select   {node|null}      — selection changed
 *   flow:node-add {node}           — a node was appended
 *   flow:node-remove {id}          — a node was removed
 * ============================================================================
 */
(function(){
  if(window.customElements && window.customElements.get('vera-flow-builder')) return;

  // Provider registry — hosts may registerFlowProvider(name, obj) and then use
  // the `provider="name"` attribute, or just call el.setProvider(obj) directly.
  window.VeraFlowProviders = window.VeraFlowProviders || {};
  window.registerFlowProvider = function(name, obj){ window.VeraFlowProviders[name] = obj; };

  function _esc(s){
    return String(s==null?'':s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }

  // Layout constants (identical to the DAG Workshop builder)
  const NODE_W = 260, NODE_H = 64, ROW_GAP = 28, COL_GAP = 24, PAD = 30;

  const STYLE = `
:host{display:block;width:100%;height:100%;color:var(--text,#e6e9ef);
  font:12px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
:host([hidden]){display:none}
*{box-sizing:border-box}
button{cursor:pointer;font:inherit;color:inherit}
input,select,textarea{font:inherit;color:var(--text,#e6e9ef);background:var(--bg1,#161a22);
  border:1px solid var(--border,#2a313e);border-radius:3px;padding:4px 7px;outline:none}
input:focus,select:focus,textarea:focus{border-color:var(--acc,#5a9e8f)}
.empty{color:var(--dim,#7a8290);font-style:italic;font-size:11px;padding:8px}
.ico{width:13px;height:13px;flex-shrink:0;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.ico-sm{width:11px;height:11px}
.btn{background:var(--bg2,#1d222c);border:1px solid var(--border,#2a313e);color:var(--text,#e6e9ef);
  border-radius:3px;padding:5px 10px;font-size:11px;display:inline-flex;align-items:center;gap:5px;transition:all .15s}
.btn:hover{background:var(--bg3,#262d3a);border-color:var(--acc,#5a9e8f)}
.btn.sm{padding:3px 7px;font-size:10.5px}
.btn.xs{padding:2px 5px;font-size:9.5px}
.btn.danger{color:var(--err,#c75a5a);border-color:rgba(199,90,90,.4)}
.btn.danger:hover{background:rgba(199,90,90,.12)}
.btn:disabled{opacity:.4;cursor:not-allowed}

/* 3-column shell */
.fb-grid{display:grid;grid-template-columns:240px 1fr 320px;gap:8px;height:100%;min-height:0}
.fb-col{background:var(--bg1,#161a22);border:1px solid var(--border,#2a313e);border-radius:var(--radius,4px);
  overflow:hidden;display:flex;flex-direction:column;min-height:0}
.fb-col-h{padding:7px 10px;border-bottom:1px solid var(--border,#2a313e);font-size:10.5px;font-weight:600;
  color:var(--text2,#aab2c0);text-transform:uppercase;letter-spacing:.4px;display:flex;align-items:center;gap:6px;background:var(--bg2,#1d222c)}
.fb-col-h .grow{flex:1}
.fb-col-body{flex:1;overflow-y:auto;padding:8px;min-height:0}

/* Palette */
.palette-search{padding:7px;border-bottom:1px solid var(--border,#2a313e);display:flex;flex-direction:column;gap:5px}
.palette-search input{width:100%;font-size:11px}
.palette-list{flex:1;overflow-y:auto;padding:6px;min-height:140px}
.palette-group{margin-bottom:6px}
.palette-group-h{font-family:var(--mono,monospace);font-size:9.5px;color:var(--dim2,#525a68);padding:3px 4px;
  display:flex;align-items:center;gap:4px;cursor:pointer;user-select:none}
.palette-group-h:hover{color:var(--text2,#aab2c0)}
.palette-group-arr{transition:transform .15s;display:inline-block}
.palette-group.collapsed .palette-group-arr{transform:rotate(-90deg)}
.palette-group.collapsed .palette-group-items{display:none}
.palette-item{padding:5px 8px;background:var(--bg2,#1d222c);border:1px solid var(--border,#2a313e);border-radius:3px;
  margin-bottom:3px;cursor:grab;font-size:10.5px;transition:all .12s}
.palette-item:hover{border-color:var(--acc,#5a9e8f);background:var(--bg3,#262d3a)}
.palette-item:active{cursor:grabbing}
.palette-item.dragging{opacity:.5}
.palette-item-name{font-family:var(--mono,monospace);color:var(--acc2,#8fb87a);font-size:10.5px;
  display:flex;justify-content:space-between;align-items:center}
.palette-item-name .req-pill{font-size:8px;color:var(--warn,#d97757);font-family:var(--mono,monospace);
  background:rgba(217,119,87,.1);padding:0 4px;border-radius:6px}
.palette-item-name .long-pill{display:inline-block;background:rgba(160,126,193,.14);color:var(--acc4,#a07ec1);
  font-size:8.5px;padding:0 5px;border-radius:6px;margin-left:4px;font-family:var(--mono,monospace)}
.palette-item-desc{font-size:10px;color:var(--dim,#7a8290);margin-top:2px;display:-webkit-box;
  -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.pal-dock-tip{flex:0 0 auto;max-height:42%;overflow-y:auto;border-top:1px solid var(--border2,#384151);
  background:var(--bg0,#0e1116);padding:8px 10px;font-family:var(--mono,monospace);font-size:10px;color:var(--text2,#aab2c0);line-height:1.5}
.pal-dock-tip .pal-dock-empty{color:var(--dim,#7a8290);font-style:italic;font-family:system-ui;font-size:10px;line-height:1.4}
.pal-dock-tip .ptip-h{font-family:system-ui;font-size:11px;color:var(--acc,#5a9e8f);font-weight:600;margin-bottom:5px;
  padding-bottom:3px;border-bottom:1px solid var(--border,#2a313e);display:flex;align-items:center;gap:6px}
.pal-dock-tip .ptip-sec{margin-top:6px;font-family:system-ui;font-size:9.5px;color:var(--dim2,#525a68);text-transform:uppercase;letter-spacing:.4px}
.pal-dock-tip .ptip-param{margin-top:3px;line-height:1.45}
.pal-dock-tip .ptip-param .pname{color:var(--acc2,#8fb87a);font-weight:500}
.pal-dock-tip .ptip-param .preq{color:var(--err,#c75a5a);margin-left:2px}
.pal-dock-tip .ptip-param .ptype{color:var(--dim2,#525a68);margin-left:4px}
.pal-dock-tip .ptip-param .penum{color:var(--acc4,#a07ec1)}
.pal-dock-tip .ptip-param .pdesc{color:var(--dim,#7a8290);font-family:system-ui;font-size:9.5px;margin-left:8px;font-style:italic}
.pal-dock-tip .long-pill{display:inline-block;background:rgba(160,126,193,.14);color:var(--acc4,#a07ec1);font-size:8.5px;padding:1px 5px;border-radius:6px;font-family:var(--mono,monospace)}

/* Canvas */
.canvas-wrap{flex:1;background:var(--bg0,#0e1116);position:relative;overflow:auto;
  background-image:radial-gradient(circle at 1px 1px, var(--bg2,#1d222c) 1px, transparent 0);background-size:24px 24px}
.canvas-svg{display:block;min-width:600px;min-height:400px}
.canvas-empty{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:var(--dim,#7a8290);
  font-size:11px;text-align:center;pointer-events:none}
.canvas-empty .ico{width:32px;height:32px;margin-bottom:4px;display:block;margin-left:auto;margin-right:auto;opacity:.6}
.canvas-wrap.drag-target{outline:2px dashed var(--acc,#5a9e8f);outline-offset:-4px}
.gnode{cursor:grab}
.gnode:active{cursor:grabbing}
.gnode-rect{fill:var(--bg2,#1d222c);stroke:var(--border2,#384151);stroke-width:1.5}
.gnode.selected .gnode-rect{stroke:var(--acc,#5a9e8f);stroke-width:2.2}
.gnode.error .gnode-rect{stroke:var(--err,#c75a5a)}
.gnode.parallel .gnode-rect{stroke:var(--warn,#d97757);stroke-dasharray:4 3}
.gnode-cap{fill:var(--acc2,#8fb87a);font-family:var(--mono,monospace);font-size:11px;pointer-events:none;font-weight:500}
.gnode-out{fill:var(--text2,#aab2c0);font-family:var(--mono,monospace);font-size:9.5px;pointer-events:none}
.gnode-args{fill:var(--dim,#7a8290);font-family:var(--mono,monospace);font-size:9px;pointer-events:none}
.gnode-idx{fill:var(--dim2,#525a68);font-family:var(--mono,monospace);font-size:9px;pointer-events:none}
.gnode-btn{cursor:pointer}
.gnode-btn .gbtn-bg{fill:var(--bg3,#262d3a);stroke:var(--border,#2a313e)}
.gnode-btn:hover .gbtn-bg{fill:var(--bg4,#2f3645)}
.gnode-btn-x .gbtn-icon{stroke:var(--err,#c75a5a);stroke-width:1.6;fill:none;stroke-linecap:round}
.gnode-btn-e .gbtn-icon{stroke:var(--text2,#aab2c0);stroke-width:1.4;fill:none}
.gedge{stroke:var(--dim2,#525a68);stroke-width:1.6;fill:none}
.gedge-arrow{fill:var(--dim2,#525a68)}
.gwire{stroke:var(--acc4,#a07ec1);stroke-width:1.2;fill:none;stroke-dasharray:4 3;opacity:.55}
.gwire-label{fill:var(--acc4,#a07ec1);font-size:9px;font-family:monospace;text-anchor:middle;opacity:.7}
.gedge.cond{stroke:var(--acc3,#c5a572);stroke-dasharray:5 3}
.gedge.cond+.gedge-arrow,.gedge-arrow.cond{fill:var(--acc3,#c5a572)}
.gnode.cond .gnode-rect{stroke:var(--acc3,#c5a572)}
.gnode-cond{fill:var(--acc3,#c5a572);font-family:var(--mono,monospace);font-size:8.5px;pointer-events:none}
.fb-state{position:absolute;top:6px;right:6px;width:210px;max-height:62%;overflow:auto;background:var(--bg0,#0e1116);border:1px solid var(--border2,#384151);border-radius:4px;padding:7px 9px;z-index:80;box-shadow:0 6px 20px rgba(0,0,0,.5)}
.fb-state-h{font-size:9px;color:var(--dim2,#525a68);text-transform:uppercase;letter-spacing:.4px;margin-bottom:5px;display:flex;justify-content:space-between}
.fb-state-row{display:flex;justify-content:space-between;gap:8px;font-family:var(--mono,monospace);font-size:9.5px;padding:1px 0;line-height:1.5}
.fb-state-k{color:var(--acc2,#8fb87a)}
.fb-state-src{color:var(--dim,#7a8290);white-space:nowrap}
.insp-om-row{display:flex;gap:4px;margin-bottom:3px;align-items:center}
.insp-om-row input{flex:1;font-size:10px;font-family:var(--mono,monospace)}
.canvas-hover-tip{position:absolute;display:none;background:var(--bg0,#0e1116);border:1px solid var(--acc,#5a9e8f);
  border-radius:3px;padding:7px 9px;font-family:var(--mono,monospace);font-size:10px;color:var(--text2,#aab2c0);
  max-width:340px;max-height:300px;overflow:auto;z-index:90;box-shadow:0 6px 20px rgba(0,0,0,.5);line-height:1.5;pointer-events:none}
.canvas-hover-tip.show{display:block}
.canvas-hover-tip .cht-h{color:var(--acc,#5a9e8f);font-weight:600;font-family:system-ui;font-size:10.5px;margin-bottom:4px;
  padding-bottom:3px;border-bottom:1px solid var(--border,#2a313e);display:flex;align-items:center;gap:6px}
.canvas-hover-tip .cht-sec{margin-top:5px;font-size:9px;color:var(--dim2,#525a68);text-transform:uppercase;letter-spacing:.4px;font-family:system-ui}
.canvas-hover-tip .cht-row{margin-top:2px;line-height:1.4}
.canvas-hover-tip .cht-row .pname{color:var(--acc2,#8fb87a)}
.canvas-hover-tip .cht-row .ptype{color:var(--dim2,#525a68);margin-left:4px}
.canvas-hover-tip .cht-row .pwire{color:var(--acc4,#a07ec1);font-style:italic;margin-left:6px;font-family:system-ui;font-size:9.5px}
.canvas-hover-tip .cht-row .plit{color:var(--ok,#5a9e8f);font-family:var(--mono,monospace);font-size:9.5px;margin-left:6px}
.long-pill{display:inline-block;background:rgba(160,126,193,.14);color:var(--acc4,#a07ec1);font-size:8.5px;padding:1px 5px;border-radius:6px;font-family:var(--mono,monospace)}

/* Inspector */
.inspector{flex:1;overflow-y:auto;padding:8px;min-height:0}
.insp-empty{padding:20px;text-align:center;color:var(--dim,#7a8290);font-size:11px}
.insp-h{font-family:var(--mono,monospace);font-size:12px;color:var(--acc,#5a9e8f);font-weight:500;margin-bottom:2px;word-break:break-word}
.insp-h .long-pill{margin-left:6px;text-transform:uppercase;letter-spacing:.4px;vertical-align:middle}
.insp-desc{font-size:10.5px;color:var(--text2,#aab2c0);margin-bottom:8px;line-height:1.4}
.insp-section{margin-bottom:10px}
.insp-section-h{font-size:9.5px;color:var(--dim2,#525a68);text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px;display:flex;align-items:center;gap:5px}
.insp-row{margin-bottom:6px}
.insp-row-label{display:flex;align-items:center;gap:4px;font-size:10.5px;color:var(--text2,#aab2c0);margin-bottom:3px;font-family:var(--mono,monospace)}
.insp-row-label .ptype{color:var(--dim2,#525a68);font-size:9.5px}
.insp-row-label .preq{color:var(--err,#c75a5a);font-size:11px;font-weight:600}
.insp-row-label .pdesc{color:var(--dim,#7a8290);font-size:9.5px;font-style:italic;margin-left:6px;font-family:system-ui}
.insp-row input,.insp-row textarea,.insp-row select{width:100%;font-size:11px}
.insp-row textarea{resize:vertical;min-height:42px;font-family:var(--mono,monospace)}
.insp-source{display:flex;gap:0;background:var(--bg2,#1d222c);border:1px solid var(--border,#2a313e);border-radius:3px;overflow:hidden;margin-bottom:3px}
.insp-source-tab{flex:1;padding:3px 6px;font-size:9.5px;color:var(--dim,#7a8290);cursor:pointer;text-align:center;border-right:1px solid var(--border,#2a313e)}
.insp-source-tab:last-child{border-right:none}
.insp-source-tab.active{background:var(--bg3,#262d3a);color:var(--acc,#5a9e8f)}
.insp-actions{display:flex;gap:5px;margin-top:10px;padding-top:8px;border-top:1px solid var(--border,#2a313e)}
`;

  const SHELL = `
<style>${STYLE}</style>
<div class="fb-grid">
  <div class="fb-col">
    <div class="fb-col-h">
      <svg class="ico ico-sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M8 12h8M12 8v8"/></svg>
      <span data-role="pal-title">Palette</span>
    </div>
    <div class="palette-search"><input data-role="pal-q" placeholder="Search…" autocomplete="off"></div>
    <div class="palette-list" data-role="pal-list"><div class="empty">Loading palette…</div></div>
    <div class="pal-dock-tip" data-role="pal-dock">
      <div class="pal-dock-empty">Hover an item to see its inputs, outputs, enum values, and constraints.</div>
    </div>
  </div>
  <div class="fb-col">
    <div class="fb-col-h">
      <svg class="ico ico-sm" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
      Canvas
      <span class="grow"></span>
      <span data-role="count" style="font-size:9.5px;color:var(--dim,#7a8290);font-family:var(--mono,monospace)">0 nodes</span>
      <button class="btn xs" data-act="state" title="Show the state table (keys nodes can read/write)">State</button>
      <button class="btn xs" data-act="auto" title="Drop manual positions">Auto-layout</button>
      <button class="btn xs" data-act="clear">Clear</button>
    </div>
    <div class="canvas-wrap" data-role="canvas-wrap">
      <svg class="canvas-svg" data-role="svg" xmlns="http://www.w3.org/2000/svg"></svg>
      <div class="canvas-hover-tip" data-role="tip"></div>
      <div class="fb-state" data-role="state-table" style="display:none"></div>
      <div class="canvas-empty" data-role="empty">
        <svg class="ico" viewBox="0 0 24 24"><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><circle cx="12" cy="18" r="3"/><line x1="6" y1="9" x2="12" y2="15"/><line x1="18" y1="9" x2="12" y2="15"/></svg>
        Drag an item from the palette<br>or click one to append it here.
      </div>
    </div>
  </div>
  <div class="fb-col">
    <div class="fb-col-h">
      <svg class="ico ico-sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
      Inspector
    </div>
    <div class="inspector" data-role="inspector">
      <div class="insp-empty">Select a node to edit it.</div>
    </div>
  </div>
</div>`;

  class VeraFlowBuilder extends HTMLElement {
    constructor(){
      super();
      this.attachShadow({mode:'open'});
      this._graph = { nodes: [], meta: {} };
      this._provider = null;
      this._sel = null;
      this._nextId = 1;
      this._palette = [];           // [{group, items:[…]}]
      this._typeIndex = {};         // type -> item
      this._schemaCache = {};       // type -> schema
      this._palTimer = null;
      this._drag = null;            // {id, startX, startY, ox, oy}
      this._dragMoved = false;
    }

    connectedCallback(){
      if(this._mounted) return;
      this._mounted = true;
      this.shadowRoot.innerHTML = SHELL;
      this._wire();
      // Allow `provider="name"` attribute via the registry.
      const pn = this.getAttribute('provider');
      if(pn && window.VeraFlowProviders[pn]) this.setProvider(window.VeraFlowProviders[pn]);
      this._renderCanvas(); this._renderInspector();
    }

    // ── element-scoped query helpers ─────────────────────────────────────────
    _$(role){ return this.shadowRoot.querySelector(`[data-role="${role}"]`); }

    // ── Public API ───────────────────────────────────────────────────────────
    setProvider(p){
      this._provider = p || null;
      this._schemaCache = {};
      const t = this._$('pal-title');
      if(t && p && p.paletteLabel) t.textContent = p.paletteLabel;
      this.refreshPalette();
      this._renderCanvas(); this._renderInspector();
    }
    getProvider(){ return this._provider; }
    getGraph(){ return this._graph; }
    setGraph(graph){
      this._graph = graph && graph.nodes ? graph : { nodes:[], meta:{} };
      this._sel = null;
      this._reindex();
      this._renderCanvas(); this._renderInspector();
    }
    getSelected(){ return this._graph.nodes.find(n=>n.id===this._sel) || null; }
    reset(){ this._graph = { nodes:[], meta:(this._graph&&this._graph.meta)||{} }; this._sel=null; this._renderCanvas(); this._renderInspector(); this._emit('flow:change'); }
    serialize(){ return this._provider && this._provider.serialize ? this._provider.serialize(this._graph) : null; }
    loadFromSource(doc){
      if(!this._provider || !this._provider.deserialize) return;
      const g = this._provider.deserialize(doc) || {nodes:[],meta:{}};
      this.setGraph(g);
    }
    async refreshPalette(){
      const host = this._$('pal-list');
      if(!this._provider || !this._provider.loadPalette){ if(host) host.innerHTML='<div class="empty">No provider</div>'; return; }
      try{
        const groups = await this._provider.loadPalette();
        this._palette = Array.isArray(groups) ? groups : [];
      }catch(e){ this._palette = []; if(host) host.innerHTML = `<div class="empty">Palette failed: ${_esc(e&&e.message||e)}</div>`; return; }
      this._typeIndex = {};
      for(const g of this._palette) for(const it of (g.items||[])){
        this._typeIndex[it.type] = it;
        if(it.schema) this._schemaCache[it.type] = it.schema;
      }
      this._renderPalette('');
    }
    autoLayout(){ this._graph.nodes.forEach(n=>{ n.pos = null; }); this._renderCanvas(); this._emit('flow:change'); }

    // ── Schema access (provider-driven, cached) ──────────────────────────────
    _schema(typeOrNode){
      const node = (typeOrNode && typeof typeOrNode==='object') ? typeOrNode : null;
      const type = node ? node.type : typeOrNode;
      if(this._provider && this._provider.schemaFor){
        const s = this._provider.schemaFor(typeOrNode);
        if(s) return s;
      }
      return this._schemaCache[type] || this._typeIndex[type]?.schema || null;
    }

    // ── Event wiring (delegation; shadow DOM has no global inline handlers) ───
    _wire(){
      const palQ = this._$('pal-q');
      palQ.addEventListener('input', ()=>{ clearTimeout(this._palTimer); this._palTimer = setTimeout(()=>this._renderPalette(palQ.value), 180); });

      const palList = this._$('pal-list');
      palList.addEventListener('click', e=>{
        const grpH = e.target.closest('.palette-group-h');
        if(grpH){ grpH.parentElement.classList.toggle('collapsed'); return; }
        const it = e.target.closest('.palette-item'); if(it) this._addNode(it.getAttribute('data-type'));
      });
      palList.addEventListener('mouseover', e=>{ const it=e.target.closest('.palette-item'); if(it) this._palDock(it.getAttribute('data-type')); });
      palList.addEventListener('dragstart', e=>{ const it=e.target.closest('.palette-item'); if(it){ e.dataTransfer.setData('text/plain', it.getAttribute('data-type')); e.dataTransfer.effectAllowed='copy'; it.classList.add('dragging'); } });
      palList.addEventListener('dragend', e=>{ const it=e.target.closest('.palette-item'); if(it) it.classList.remove('dragging'); });

      // Header buttons
      this.shadowRoot.querySelector('[data-act="clear"]').addEventListener('click', ()=>this._clear());
      this.shadowRoot.querySelector('[data-act="auto"]').addEventListener('click', ()=>this.autoLayout());
      this.shadowRoot.querySelector('[data-act="state"]').addEventListener('click', ()=>this._toggleStateTable());

      // Canvas drop target
      const wrap = this._$('canvas-wrap');
      wrap.addEventListener('dragover', e=>{ e.preventDefault(); e.dataTransfer.dropEffect='copy'; wrap.classList.add('drag-target'); });
      wrap.addEventListener('dragleave', ()=>wrap.classList.remove('drag-target'));
      wrap.addEventListener('drop', e=>{ e.preventDefault(); wrap.classList.remove('drag-target'); const t=e.dataTransfer.getData('text/plain'); if(t) this._addNode(t); });

      // Canvas node interactions (click select / buttons / hover tip / drag-move)
      const svg = this._$('svg');
      svg.addEventListener('click', e=>{
        if(this._dragMoved){ this._dragMoved=false; return; }
        const btn = e.target.closest('[data-act]');
        if(btn){ e.stopPropagation(); const id=btn.getAttribute('data-id');
          if(btn.getAttribute('data-act')==='del') this._deleteNode(id); else this._select(id); return; }
        const g = e.target.closest('.gnode'); if(g) this._select(g.getAttribute('data-id'));
      });
      svg.addEventListener('mousedown', e=>{
        if(e.button!==0) return;
        if(e.target.closest('[data-act]')) return;   // button, not a drag
        const g = e.target.closest('.gnode'); if(!g) return;
        const id = g.getAttribute('data-id');
        const pos = this._nodeXY && this._nodeXY[id]; if(!pos) return;
        const u = this._toUser(e);
        this._drag = { id, dx: u.x - pos.x, dy: u.y - pos.y };
        this._dragMoved = false;
      });
      svg.addEventListener('mousemove', e=>{
        const wrapEl = this._$('canvas-wrap');
        // hover tooltip (only when not dragging)
        if(!this._drag){
          const g = e.target.closest('.gnode');
          if(g) this._nodeHover(g.getAttribute('data-id'), e); else this._nodeHoverHide();
        }
        if(!this._drag) return;
        const u = this._toUser(e);
        const node = this._graph.nodes.find(n=>n.id===this._drag.id); if(!node) return;
        node.pos = { x: Math.max(0, u.x - this._drag.dx), y: Math.max(0, u.y - this._drag.dy) };
        this._dragMoved = true;
        this._renderCanvas();
      });
      svg.addEventListener('mouseleave', ()=>this._nodeHoverHide());
      window.addEventListener('mouseup', ()=>{ if(this._drag){ this._drag=null; this._emit('flow:change'); } });

      // Inspector delegated edits (for the built-in inspector)
      const insp = this._$('inspector');
      insp.addEventListener('click', e=>{
        const tab = e.target.closest('.insp-source-tab[data-act="src"]');
        if(tab){ this._setParamSource(tab.getAttribute('data-node'), tab.getAttribute('data-param'), tab.getAttribute('data-src')); return; }
        const act = e.target.closest('[data-act]'); if(!act) return;
        const a = act.getAttribute('data-act');
        if(a==='dup') this._duplicateNode(act.getAttribute('data-node'));
        else if(a==='del') this._deleteNode(act.getAttribute('data-node'));
        else if(a==='omadd') this._omAdd(act.getAttribute('data-node'));
        else if(a==='omdel') this._omDel(act.getAttribute('data-node'), act.getAttribute('data-i'));
      });
      insp.addEventListener('input', e=>{ this._inspInput(e); });
      insp.addEventListener('change', e=>{ this._inspInput(e); });
    }

    _inspInput(e){
      const f = e.target.closest('[data-act]'); if(!f) return;
      const a = f.getAttribute('data-act');
      const id = f.getAttribute('data-node');
      const node = this._graph.nodes.find(n=>n.id===id); if(!node) return;
      if(a==='out'){ node.out = f.value.trim(); this._renderCanvas(); this._emit('flow:change'); }
      else if(a==='val'){ const p=f.getAttribute('data-param'); (node.params[p]=node.params[p]||{source:'value'}).value=f.value; (node.params[p]).source='value'; this._renderCanvas(); this._emit('flow:change'); }
      else if(a==='ref'){ const p=f.getAttribute('data-param'); (node.params[p]=node.params[p]||{source:'state'}).value=f.value; (node.params[p]).source='state'; this._renderCanvas(); this._emit('flow:change'); }
      else if(a==='cond'){ node.condition = f.value; this._renderCanvas(); this._emit('flow:change'); }
      else if(a==='omk' || a==='omv'){ this._rebuildOutputMap(node); this._renderCanvas(); this._emit('flow:change'); }
    }
    // ── Output-extraction (output_map) editing ──────────────────────────────
    _omAdd(id){
      const n = this._graph.nodes.find(x=>x.id===id); if(!n) return;
      n.output_map = n.output_map || {};
      let k = 'field'+(Object.keys(n.output_map).length+1); while(k in n.output_map) k+='_';
      n.output_map[k] = ''; this._renderInspector();
    }
    _omDel(id, i){
      const n = this._graph.nodes.find(x=>x.id===id); if(!n || !n.output_map) return;
      const key = Object.keys(n.output_map)[parseInt(i,10)];
      if(key!=null) delete n.output_map[key];
      if(!Object.keys(n.output_map).length) n.output_map = null;
      this._renderInspector(); this._renderCanvas(); this._emit('flow:change');
    }
    _rebuildOutputMap(n){
      const host = this._$('inspector'); const om = {};
      host.querySelectorAll('[data-omr]').forEach(row=>{
        const k = row.querySelector('[data-act="omk"]'), v = row.querySelector('[data-act="omv"]');
        if(k && k.value.trim()) om[k.value.trim()] = v ? v.value.trim() : '';
      });
      n.output_map = Object.keys(om).length ? om : null;
    }

    // ── Palette render ───────────────────────────────────────────────────────
    _renderPalette(query){
      const host = this._$('pal-list'); if(!host) return;
      const q = (query||'').toLowerCase().trim();
      let html = '';
      for(const g of this._palette){
        const items = q ? (g.items||[]).filter(it =>
          (it.type||'').toLowerCase().includes(q) ||
          (it.label||'').toLowerCase().includes(q) ||
          (it.description||'').toLowerCase().includes(q)) : (g.items||[]);
        if(!items.length) continue;
        html += `<div class="palette-group" data-group="${_esc(g.group)}">
          <div class="palette-group-h">
            <span class="palette-group-arr">▾</span>
            <span style="color:var(--acc4,#a07ec1)">${_esc(g.group)}</span>
            <span style="color:var(--dim2,#525a68);margin-left:auto">${items.length}</span>
          </div>
          <div class="palette-group-items">
            ${items.map(it=>{
              const sc = it.schema || this._schemaCache[it.type] || {};
              const reqCount = (sc.params||[]).filter(p=>p.required).length;
              const reqPill = reqCount ? `<span class="req-pill">${reqCount} req</span>` : '';
              const longPill = (it.long_running||sc.long_running) ? `<span class="long-pill">long</span>` : '';
              return `<div class="palette-item" draggable="true" data-type="${_esc(it.type)}">
                <div class="palette-item-name"><span>${_esc(it.label||it.type)}${longPill}</span>${reqPill}</div>
                <div class="palette-item-desc">${_esc(it.description||sc.description||'')}</div>
              </div>`;
            }).join('')}
          </div>
        </div>`;
      }
      host.innerHTML = html || '<div class="empty">No matches</div>';
    }

    _palDock(type){
      const dock = this._$('pal-dock'); if(!dock) return;
      const it = this._typeIndex[type]; const sc = this._schema(type) || (it&&it.schema) || {};
      const params = sc.params || [];
      let html = `<div class="ptip-h">${_esc(it&&it.label||type)}${(it&&it.long_running||sc.long_running)?' <span class="long-pill">long-running</span>':''}</div>`;
      const desc = (it&&it.description)||sc.description;
      if(desc) html += `<div style="color:var(--dim,#7a8290);font-family:system-ui;font-size:9.5px;margin-bottom:4px">${_esc(desc)}</div>`;
      if(params.length){
        html += `<div class="ptip-sec">Inputs (${params.filter(p=>p.required).length} required)</div>`;
        for(const p of params){
          const reqMark = p.required ? `<span class="preq">!</span>` : '';
          const def = (p.default!==undefined && p.default!==null) ? ` (default: ${_esc(JSON.stringify(p.default).slice(0,50))})` : '';
          html += `<div class="ptip-param"><span class="pname">${_esc(p.name)}</span><span class="ptype">: ${_esc(p.type||'string')}</span>${reqMark}${def}`;
          if(p.description) html += `<div class="pdesc">${_esc(p.description)}</div>`;
          if(p.enum && p.enum.length) html += `<div class="pdesc"><span class="penum">valid:</span> ${p.enum.map(e=>_esc(JSON.stringify(e))).join(' | ')}</div>`;
          html += `</div>`;
        }
      }
      const outs = sc.outputs || [];
      if(outs.length){
        html += `<div class="ptip-sec">Writes</div>`;
        for(const o of outs) html += `<div class="ptip-param"><span class="pname">${_esc(o.name)}</span>${o.description?`<span class="pdesc">— ${_esc(o.description)}</span>`:''}</div>`;
      }
      if(!params.length && !outs.length) html += `<div class="pal-dock-empty">No declared params or outputs.</div>`;
      dock.innerHTML = html;
    }

    // ── Model ops ────────────────────────────────────────────────────────────
    _reindex(){
      let max = 0;
      for(const n of this._graph.nodes){ const m = parseInt(String(n.id).replace(/^n/,''),10); if(!isNaN(m)&&m>max) max=m; }
      this._nextId = max + 1;
    }
    _stateKeysBefore(idx){
      const keys = new Set(Object.keys(this._graph.meta&&this._graph.meta.initial_state||{}));
      for(const n of this._graph.nodes.slice(0, idx)){
        if(n.out) keys.add(n.out);
        if(n.output_map) for(const k of Object.keys(n.output_map)) keys.add(k);
        for(const [pn, ps] of Object.entries(n.params||{})) if(ps.source==='value' && ps.value!=='' && ps.value!=null) keys.add(pn);
      }
      return keys;
    }
    _defaultNode(item){
      const id = 'n' + (this._nextId++);
      const sc = item.schema || this._schema(item.type) || {};
      const base = (item.type||'node').split('.').pop().replace(/[^a-z0-9_]/gi,'_');
      const params = {};
      const existing = this._stateKeysBefore(this._graph.nodes.length);
      for(const p of (sc.params||[])){
        let src='value', val='';
        if(existing.has(p.name)){ src='state'; val=p.name; }
        else if(p.default!==undefined && p.default!==null){ val=p.default; }
        params[p.name] = { source:src, value:val };
      }
      return { id, type:item.type, out: base+'_'+id, params, output_map:null, condition:'', parallel_with:null, pos:null, meta:{} };
    }
    _addNode(type){
      const item = this._typeIndex[type]; if(!item){ return; }
      const node = (this._provider && this._provider.createNode)
        ? this._provider.createNode(item, this._ctx())
        : this._defaultNode(item);
      if(!node.id) node.id = 'n'+(this._nextId++);
      if(node.pos===undefined) node.pos = null;
      this._graph.nodes.push(node);
      this._sel = node.id;
      this._renderCanvas(); this._renderInspector();
      this._emit('flow:node-add', { node });
      this._emit('flow:change');
    }
    _duplicateNode(id){
      const n = this._graph.nodes.find(x=>x.id===id); if(!n) return;
      const copy = JSON.parse(JSON.stringify(n));
      copy.id = 'n'+(this._nextId++);
      copy.parallel_with = null;
      if(copy.pos) copy.pos = { x: copy.pos.x+24, y: copy.pos.y+24 };
      const idx = this._graph.nodes.indexOf(n);
      this._graph.nodes.splice(idx+1, 0, copy);
      this._sel = copy.id;
      this._renderCanvas(); this._renderInspector(); this._emit('flow:change');
    }
    _deleteNode(id){
      const n = this._graph.nodes.find(x=>x.id===id); if(!n) return;
      this._graph.nodes.forEach(x=>{ if(x.parallel_with===id) x.parallel_with=null; });
      this._graph.nodes = this._graph.nodes.filter(x=>x.id!==id);
      if(this._sel===id) this._sel=null;
      this._renderCanvas(); this._renderInspector();
      this._emit('flow:node-remove', { id });
      this._emit('flow:change');
    }
    _clear(){
      if(this._graph.nodes.length && !confirm('Clear all nodes?')) return;
      this._graph.nodes = []; this._sel=null; this._renderCanvas(); this._renderInspector(); this._emit('flow:change');
    }
    _select(id){ this._sel = id; this._renderCanvas(); this._renderInspector(); this._emit('flow:select', { node: this.getSelected() }); }
    _setParamSource(id, pname, src){
      const n = this._graph.nodes.find(x=>x.id===id); if(!n) return;
      (n.params[pname] = n.params[pname]||{value:''}).source = src;
      this._renderInspector(); this._renderCanvas(); this._emit('flow:change');
    }

    _ctx(){
      return { el:this, graph:this._graph, esc:_esc,
        schema:(t)=>this._schema(t),
        // Full refresh (rebuilds the inspector — use on structural changes).
        update:()=>{ this._renderCanvas(); this._renderInspector(); this._emit('flow:change'); },
        // Light refresh: re-render canvas + notify, but DON'T rebuild the
        // inspector — so a focused field (e.g. a JSON config textarea) keeps
        // its caret/focus while the user types.
        refresh:()=>{ this._renderCanvas(); this._emit('flow:change'); },
        select:(id)=>this._select(id) };
    }
    _emit(name, detail){
      this.dispatchEvent(new CustomEvent(name, { detail: Object.assign({ graph:this._graph }, detail||{}), bubbles:true, composed:true }));
      if(name==='flow:change' && this._provider && this._provider.onChange){ try{ this._provider.onChange(this._graph); }catch(_){} }
    }

    // ── Validation ───────────────────────────────────────────────────────────
    _issues(n){
      // A provider that supplies validateNode is authoritative (consulted before
      // the schema-presence guard, so providers without param schemas — e.g.
      // dream stages — don't flash spurious "unknown type" errors).
      if(this._provider && this._provider.validateNode){ try{ return this._provider.validateNode(n, this._schema(n))||[]; }catch(_){ } }
      const sc = this._schema(n); if(!sc) return ['unknown type'];
      const issues = [];
      for(const p of (sc.params||[]).filter(p=>p.required)){
        const ps = n.params?.[p.name];
        if(!ps) issues.push(`${p.name} required`);
        else if(ps.source==='value' && (ps.value===''||ps.value==null)) issues.push(`${p.name} required`);
        else if(ps.source==='state' && !ps.value) issues.push(`${p.name} unset`);
      }
      return issues;
    }
    _argsSummary(n){
      const sc = this._schema(n); if(!sc) return '';
      const idx = this._graph.nodes.indexOf(n);
      const srcMap = {};
      for(let i=0;i<idx;i++){ const u=this._graph.nodes[i]; if(u.out) srcMap[u.out]='#'+(i+1); if(u.output_map) for(const k of Object.keys(u.output_map)) srcMap[k]='#'+(i+1); }
      const parts = [];
      for(const p of (sc.params||[])){
        const ps = n.params?.[p.name]; if(!ps) continue;
        if(ps.source==='state' && ps.value){ parts.push(srcMap[ps.value] ? `${ps.value}←${srcMap[ps.value]}` : `${p.name}←${ps.value}`); }
        else if(ps.source==='value' && ps.value!=='' && ps.value!=null){ const v=String(ps.value).slice(0,12); parts.push(`${p.name}=${v}${String(ps.value).length>12?'…':''}`); }
      }
      let s = parts.join('  '); if(s.length>38) s=s.slice(0,38)+'…'; return s;
    }

    // ── Canvas render (auto-layout + manual position override) ────────────────
    _toUser(e){
      const svg = this._$('svg');
      const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
      const m = svg.getScreenCTM(); if(!m) return {x:0,y:0};
      const u = pt.matrixTransform(m.inverse()); return { x:u.x, y:u.y };
    }
    _rows(){
      const rows=[]; const seen=new Set();
      for(const n of this._graph.nodes){
        if(seen.has(n.id)) continue;
        if(n.parallel_with){
          const peers = this._graph.nodes.filter(x=>x.parallel_with===n.parallel_with || x.id===n.parallel_with);
          peers.forEach(p=>seen.add(p.id)); rows.push(peers);
        } else {
          const peers = this._graph.nodes.filter(x=>x.parallel_with===n.id);
          if(peers.length){ seen.add(n.id); peers.forEach(p=>seen.add(p.id)); rows.push([n,...peers]); }
          else { seen.add(n.id); rows.push([n]); }
        }
      }
      return rows;
    }
    _renderCanvas(){
      const svg = this._$('svg'), empty = this._$('empty'), counter = this._$('count');
      if(!svg) return;
      counter.textContent = this._graph.nodes.length + ' node' + (this._graph.nodes.length===1?'':'s');
      if(!this._graph.nodes.length){ svg.innerHTML=''; empty.style.display='block'; this._nodeXY={}; return; }
      empty.style.display='none';

      const rows = this._rows();
      // Auto-layout positions, then override with manual node.pos
      const autoW = Math.max(600, PAD*2 + rows.reduce((m,r)=>Math.max(m, r.length*NODE_W + (r.length-1)*COL_GAP),0));
      const nodeXY = {};
      rows.forEach((row, ri)=>{
        const rowW = row.length*NODE_W + (row.length-1)*COL_GAP;
        const startX = (autoW - rowW)/2;
        const y = PAD + ri*(NODE_H + ROW_GAP);
        row.forEach((n, ci)=>{
          const x = startX + ci*(NODE_W + COL_GAP);
          nodeXY[n.id] = (n.pos && typeof n.pos.x==='number')
            ? { x:n.pos.x, y:n.pos.y, cx:n.pos.x+NODE_W/2, cy:n.pos.y+NODE_H/2 }
            : { x, y, cx:x+NODE_W/2, cy:y+NODE_H/2 };
        });
      });
      // Canvas extent fits both auto rows and manual placements
      let W = autoW, H = PAD*2 + rows.length*NODE_H + (rows.length-1)*ROW_GAP;
      for(const id in nodeXY){ W=Math.max(W, nodeXY[id].x+NODE_W+PAD); H=Math.max(H, nodeXY[id].y+NODE_H+PAD); }
      svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('width', W); svg.setAttribute('height', H);
      this._nodeXY = nodeXY;

      // Node cards
      const elements = [];
      this._graph.nodes.forEach((n)=>{
        const P = nodeXY[n.id]; if(!P) return;
        const isSel = this._sel===n.id;
        const isParallel = !!(n.parallel_with) || this._graph.nodes.some(x=>x.parallel_with===n.id);
        const issues = this._issues(n);
        const cls = `gnode${isSel?' selected':''}${issues.length?' error':''}${isParallel?' parallel':''}${n.condition?' cond':''}`;
        const card = (this._provider && this._provider.nodeCard) ? this._provider.nodeCard(n, this._schema(n)) : null;
        const title = card&&card.title!=null ? card.title : n.type;
        const outLine = card&&card.line!=null ? card.line : ('→ ' + (n.out||'(no key)'));
        const argsLine = card&&card.sub!=null ? card.sub : this._argsSummary(n);
        elements.push(`<g class="${cls}" data-id="${_esc(n.id)}">
          <rect class="gnode-rect" x="${P.x}" y="${P.y}" width="${NODE_W}" height="${NODE_H}" rx="5"/>
          <text class="gnode-idx"  x="${P.x+8}" y="${P.y+13}">#${this._graph.nodes.indexOf(n)+1}${isParallel?' · parallel':''}${n.condition?` · ⌥if ${_esc(n.condition)}`:''}</text>
          <text class="gnode-cap"  x="${P.x+8}" y="${P.y+30}">${_esc(title)}</text>
          <text class="gnode-out"  x="${P.x+8}" y="${P.y+45}">${_esc(outLine)}</text>
          <text class="gnode-args" x="${P.x+8}" y="${P.y+58}">${_esc(argsLine)}</text>
          <g class="gnode-btn gnode-btn-e" data-act="edit" data-id="${_esc(n.id)}" transform="translate(${P.x+NODE_W-44},${P.y+6})">
            <rect class="gbtn-bg" width="16" height="16" rx="3"/><path class="gbtn-icon" d="M4 11l8-8 1 1-8 8H4z M3 12l1.5 0.5"/></g>
          <g class="gnode-btn gnode-btn-x" data-act="del" data-id="${_esc(n.id)}" transform="translate(${P.x+NODE_W-22},${P.y+6})">
            <rect class="gbtn-bg" width="16" height="16" rx="3"/><path class="gbtn-icon" d="M5 5l6 6 M11 5l-6 6"/></g>
        </g>`);
      });

      // Edges: row → next row
      const edges = [];
      for(let i=0;i<rows.length-1;i++){
        for(const a of rows[i]) for(const b of rows[i+1]){
          const A=nodeXY[a.id], B=nodeXY[b.id]; if(!A||!B) continue;
          const x1=A.cx, y1=A.y+NODE_H, x2=B.cx, y2=B.y, my=(y1+y2)/2;
          const cc = b.condition ? ' cond' : '';
          edges.push(`<path class="gedge${cc}" d="M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2-5}"/>
            <polygon class="gedge-arrow${cc}" points="${x2-4},${y2-5} ${x2+4},${y2-5} ${x2},${y2}"/>`);
        }
      }
      // Data-wires: producer out-key → consumer state-param referencing it
      const wires = [];
      const producer = {};
      this._graph.nodes.forEach(n=>{ if(n.out) producer[n.out]=n.id; if(n.output_map) for(const k of Object.keys(n.output_map)) producer[k]=producer[k]||n.id; });
      for(const consumer of this._graph.nodes){
        const C = nodeXY[consumer.id]; if(!C) continue;
        for(const [, ps] of Object.entries(consumer.params||{})){
          if(ps.source!=='state' || !ps.value) continue;
          const prodId = producer[ps.value]; if(!prodId || prodId===consumer.id) continue;
          const Pp = nodeXY[prodId]; if(!Pp) continue;
          const cRow = rows.findIndex(r=>r.find(x=>x.id===consumer.id));
          const pRow = rows.findIndex(r=>r.find(x=>x.id===prodId));
          if(cRow-pRow===1 && rows[pRow].length===1 && rows[cRow].length===1) continue;
          const x1=Pp.x+NODE_W-6, y1=Pp.cy, x2=C.x+6, y2=C.cy, dx=Math.abs(x2-x1)*0.4+30;
          wires.push(`<path class="gwire" d="M${x1},${y1} C${x1+dx},${y1} ${x2-dx},${y2} ${x2},${y2}"/>
            <text class="gwire-label" x="${(x1+x2)/2}" y="${(y1+y2)/2-3}">${_esc(ps.value)}</text>`);
        }
      }
      svg.innerHTML = edges.join('') + wires.join('') + elements.join('');
      if(this._stateOpen) this._renderStateTable();
    }

    // ── State table — keys nodes can read/write (the backend state dict) ──────
    _toggleStateTable(){
      const host = this._$('state-table'); if(!host) return;
      this._stateOpen = !this._stateOpen;
      host.style.display = this._stateOpen ? 'block' : 'none';
      if(this._stateOpen) this._renderStateTable();
    }
    _renderStateTable(){
      const host = this._$('state-table'); if(!host) return;
      const rows = [];
      const init = (this._graph.meta && this._graph.meta.initial_state) || {};
      for(const k of Object.keys(init)) rows.push([k, 'initial']);
      this._graph.nodes.forEach((n,i)=>{
        if(n.out) rows.push([n.out, `#${i+1} ${n.type}`]);
        if(n.output_map) for(const k of Object.keys(n.output_map)) rows.push([k, `#${i+1} ${n.type} ▹`]);
      });
      host.innerHTML = `<div class="fb-state-h"><span>State table</span><span>${rows.length} key${rows.length===1?'':'s'}</span></div>` +
        (rows.length
          ? rows.map(([k,src])=>`<div class="fb-state-row"><span class="fb-state-k">${_esc(k)}</span><span class="fb-state-src">${_esc(src)}</span></div>`).join('')
          : '<div class="pal-dock-empty">No state keys yet — give nodes an output key.</div>');
    }

    // ── DAG export — graph → {dag tuples, initial state} for /workshop/dag/run_stream ──
    toDag(){
      const groups = []; const seen = new Set();
      for(const n of this._graph.nodes){
        if(seen.has(n.id)) continue;
        if(n.parallel_with){
          const peers = this._graph.nodes.filter(x=>x.parallel_with===n.parallel_with || x.id===n.parallel_with);
          peers.forEach(p=>seen.add(p.id)); groups.push(peers);
        } else {
          const peers = this._graph.nodes.filter(x=>x.parallel_with===n.id);
          if(peers.length){ seen.add(n.id); peers.forEach(p=>seen.add(p.id)); groups.push([n,...peers]); }
          else { seen.add(n.id); groups.push([n]); }
        }
      }
      const dag = groups.map(g => g.length===1 ? this._nodeToTuple(g[0]) : g.map(x=>this._nodeToTuple(x)));
      return { dag, state: this._buildInitialState() };
    }
    _nodeToTuple(n){
      const im = {};
      for(const [pn, ps] of Object.entries(n.params||{})){ if(ps.source==='state' && ps.value && ps.value!==pn) im[pn]=ps.value; }
      const tuple = [n.type, n.out||null];
      const cond = n.condition ? ('CONDITION:'+n.condition) : null;
      const input_map  = Object.keys(im).length ? im : null;
      const output_map = (n.output_map && Object.keys(n.output_map).length) ? n.output_map : null;
      if(cond || input_map || output_map) tuple.push(cond);
      if(input_map || output_map) tuple.push(input_map);
      if(output_map) tuple.push(output_map);
      return tuple;
    }
    _buildInitialState(){
      const state = Object.assign({}, (this._graph.meta && this._graph.meta.initial_state) || {});
      for(const n of this._graph.nodes){
        for(const [pn, ps] of Object.entries(n.params||{})){
          if(ps.source==='value' && ps.value!=='' && ps.value!=null && !(pn in state)){
            state[pn] = this._coerce(ps.value, this._paramType(n, pn));
          }
        }
      }
      return state;
    }
    _paramType(n, pn){ const sc=this._schema(n); if(!sc) return 'string'; const p=(sc.params||[]).find(x=>x.name===pn); return (p&&p.type)||'string'; }
    _coerce(v, t){
      if(v===''||v==null) return v;
      if(t==='integer'){ const n=parseInt(v,10); return isNaN(n)?v:n; }
      if(t==='number'){ const n=parseFloat(v); return isNaN(n)?v:n; }
      if(t==='boolean') return v===true||v==='true'||v===1||v==='1';
      if(t==='array'||t==='object'){ try{ return JSON.parse(v); }catch(_){ return v; } }
      return v;
    }

    // ── Run the graph through the shared DAG engine (/workshop/dag/run_stream) ──
    // opts: { base:'' (orchestrator origin), state:{} (extra initial state),
    //         into: HTMLElement (optional — renders a simple per-node log) }
    // Streams SSE; dispatches `flow:run` CustomEvents {phase, event?}.
    async runAsDag(opts){
      opts = opts || {};
      const { dag, state } = this.toDag();
      const log = opts.into || null;
      const _line = (txt, cls)=>{ if(!log) return; const d=document.createElement('div'); d.textContent=txt;
        d.style.cssText='font-family:ui-monospace,monospace;font-size:10px;padding:1px 0;color:'+(cls==='err'?'#c75a5a':cls==='ok'?'#5a9e8f':'inherit');
        log.appendChild(d); log.scrollTop=log.scrollHeight; };
      if(log) log.innerHTML = '';
      if(!Array.isArray(dag) || !dag.length){ _line('Nothing to run — add at least one cap node.','err'); this._emit('flow:run',{phase:'error',error:'empty'}); return; }
      const body = JSON.stringify({ dag, state: Object.assign({}, state, opts.state||{}) });
      this._emit('flow:run',{phase:'start', dag, state});
      _line('▶ running '+dag.length+' node'+(dag.length===1?'':'s')+'…');
      let res;
      try{ res = await fetch((opts.base||'')+'/workshop/dag/run_stream', { method:'POST', headers:{'Content-Type':'application/json'}, body }); }
      catch(e){ _line('✗ '+(e&&e.message||e),'err'); this._emit('flow:run',{phase:'error',error:String(e)}); return; }
      if(!res.ok || !res.body){ _line('✗ HTTP '+res.status,'err'); this._emit('flow:run',{phase:'error',error:'HTTP '+res.status}); return; }
      const reader = res.body.getReader(); const dec = new TextDecoder(); let buf='';
      const handle = (ev)=>{
        this._emit('flow:run',{phase:'event', event:ev});
        if(ev.type==='node_start') _line('▸ #'+((ev.index||0)+1)+' '+(ev.cap||''));
        else if(ev.type==='node_done') _line('  ✓ '+(ev.cap||'')+(ev.preview?' — '+String(ev.preview).slice(0,80):''),'ok');
        else if(ev.type==='node_error'||ev.type==='error') _line('  ✗ '+(ev.error||ev.cap||'error'),'err');
        else if(ev.type==='done'||ev.type==='complete') _line('✓ done','ok');
      };
      try{
        while(true){
          const { value, done } = await reader.read(); if(done) break;
          buf += dec.decode(value, {stream:true});
          let i; while((i=buf.indexOf('\n\n'))>=0){
            const chunk = buf.slice(0,i); buf = buf.slice(i+2);
            const dl = chunk.split('\n').find(l=>l.startsWith('data:')); if(!dl) continue;
            const data = dl.slice(5).trim();
            if(data==='[DONE]'){ continue; }
            let ev; try{ ev = JSON.parse(data); }catch(_){ continue; }
            handle(ev);
          }
        }
      }catch(e){ _line('✗ stream: '+(e&&e.message||e),'err'); }
      this._emit('flow:run',{phase:'done'});
    }

    _nodeHover(id, e){
      const tip = this._$('tip'), wrap = this._$('canvas-wrap'); if(!tip||!wrap) return;
      const n = this._graph.nodes.find(x=>x.id===id); if(!n){ tip.classList.remove('show'); return; }
      const sc = this._schema(n) || {};
      const idx = this._graph.nodes.indexOf(n);
      const srcMap = {};
      for(let i=0;i<idx;i++){ const u=this._graph.nodes[i]; if(u.out) srcMap[u.out]=`#${i+1} ${u.type}`; if(u.output_map) for(const k of Object.keys(u.output_map)) srcMap[k]=srcMap[k]||`#${i+1} ${u.type}`; }
      let inputs='';
      const params = sc.params||[];
      if(params.length){
        inputs += '<div class="cht-sec">Inputs</div>';
        for(const p of params){
          const ps = n.params?.[p.name]; const req = p.required?' <span style="color:var(--err,#c75a5a)">*</span>':'';
          let val='';
          if(ps){ if(ps.source==='state'&&ps.value){ val = srcMap[ps.value]?`<span class="pwire">← from ${_esc(srcMap[ps.value])}.${_esc(ps.value)}</span>`:`<span class="pwire">← state.${_esc(ps.value)}</span>`; }
            else if(ps.source==='value'&&ps.value!==''&&ps.value!=null){ const lit=String(ps.value); val=`<span class="plit">= ${_esc(lit.length>40?lit.slice(0,40)+'…':lit)}</span>`; } }
          inputs += `<div class="cht-row"><span class="pname">${_esc(p.name)}</span>${req}<span class="ptype">${_esc(p.type||'any')}</span>${val}</div>`;
        }
      }
      let outputs='';
      if(n.out || (sc.output_keys||[]).length){
        outputs += '<div class="cht-sec">Outputs</div>';
        if(n.out) outputs += `<div class="cht-row"><span class="pname">→ ${_esc(n.out)}</span><span class="ptype">whole result</span></div>`;
        if(n.output_map) for(const [k,v] of Object.entries(n.output_map)) outputs += `<div class="cht-row"><span class="pname">→ ${_esc(k)}</span><span class="ptype">from result.${_esc(v)}</span></div>`;
      }
      tip.innerHTML = `<div class="cht-h">${_esc(n.type)}</div>${sc.description?`<div style="color:var(--dim,#7a8290);font-family:system-ui;font-size:9.5px;margin-bottom:3px;font-style:italic">${_esc(sc.description.slice(0,180))}</div>`:''}${inputs}${outputs}`;
      tip.classList.add('show');
      const rect = wrap.getBoundingClientRect();
      let mx=(e.clientX-rect.left)+14, my=(e.clientY-rect.top)+12;
      const tw=tip.offsetWidth||320, th=tip.offsetHeight||200;
      if(mx+tw>rect.width-8) mx=rect.width-tw-8; if(my+th>rect.height-8) my=rect.height-th-8;
      tip.style.left=Math.max(4,mx)+'px'; tip.style.top=Math.max(4,my)+'px';
    }
    _nodeHoverHide(){ const tip=this._$('tip'); if(tip) tip.classList.remove('show'); }

    // ── Inspector render (built-in schema editor, or provider override) ───────
    _renderInspector(){
      const host = this._$('inspector'); if(!host) return;
      const node = this.getSelected();
      if(!node){
        let html = '<div class="insp-empty">Select a node to edit it.</div>';
        if(this._provider && this._provider.globalSection){ try{ html = this._provider.globalSection(this._graph, this._ctx()) || html; }catch(_){} }
        host.innerHTML = html;
        if(this._provider && this._provider.bindGlobalSection){ try{ this._provider.bindGlobalSection(host, this._ctx()); }catch(_){} }
        return;
      }
      if(this._provider && this._provider.renderInspector){
        const r = this._provider.renderInspector(node, this._ctx());
        const providerHtml = (typeof r === 'string') ? r : (r && r.html!=null ? r.html : null);
        if(providerHtml != null){
          // Append the generic wiring + control section after the provider's
          // domain config, so wiring/conditions/output-keys are available on
          // every node. Providers opt out with `wiring === false`.
          const wiring = (this._provider.wiring===false) ? ''
            : `<div class="insp-section-h" style="margin-top:8px;border-top:1px solid var(--border,#2a313e);padding-top:8px">⇄ Wiring &amp; control</div>${this._wiringHtml(node)}`;
          host.innerHTML = providerHtml + wiring + this._actionsHtml(node);
          if(r && r.bind){ try{ r.bind(host); }catch(_){} }
          return;
        }
      }
      host.innerHTML = this._defaultInspectorHtml(node);
    }
    // Shared "data wiring + control" section: inputs (literal ⇄ from-state),
    // output key, output extraction (output_map), and run condition. Rendered
    // by the default inspector AND appended after any provider inspector, so
    // every node — caps and domain stages alike — can be wired and gated.
    _wiringHtml(node){
      const sc = this._schema(node) || {};
      const idx = this._graph.nodes.indexOf(node);
      const refKeys = [...this._stateKeysBefore(idx)];
      let inputs = (this._provider && this._provider.nodeInputs) ? this._provider.nodeInputs(node) : null;
      if(!inputs) inputs = sc.params || [];
      let rows = '';
      for(const p of inputs){
        const ps = node.params[p.name] || { source:'value', value:'' };
        const isState = ps.source==='state';
        let control;
        if(isState){
          const opts = ['<option value="">— pick a key —</option>'].concat(refKeys.map(k=>`<option value="${_esc(k)}"${k===ps.value?' selected':''}>${_esc(k)}</option>`)).join('');
          control = `<select data-act="ref" data-node="${_esc(node.id)}" data-param="${_esc(p.name)}">${opts}</select>`;
        } else if(p.enum && p.enum.length){
          const opts = p.enum.map(e=>`<option value="${_esc(e)}"${String(e)===String(ps.value)?' selected':''}>${_esc(e)}</option>`).join('');
          control = `<select data-act="val" data-node="${_esc(node.id)}" data-param="${_esc(p.name)}"><option value="">—</option>${opts}</select>`;
        } else if(p.type==='object' || p.type==='array'){
          control = `<textarea data-act="val" data-node="${_esc(node.id)}" data-param="${_esc(p.name)}" placeholder="${_esc(p.type)} JSON">${_esc(ps.value!=null?ps.value:'')}</textarea>`;
        } else {
          control = `<input data-act="val" data-node="${_esc(node.id)}" data-param="${_esc(p.name)}" value="${_esc(ps.value!=null?ps.value:'')}" placeholder="${_esc(p.type||'value')}">`;
        }
        rows += `<div class="insp-row">
          <div class="insp-row-label">${_esc(p.name)}<span class="ptype">${_esc(p.type||'string')}</span>${p.required?'<span class="preq">*</span>':''}${p.description?`<span class="pdesc">${_esc(p.description)}</span>`:''}</div>
          <div class="insp-source">
            <div class="insp-source-tab${!isState?' active':''}" data-act="src" data-src="value" data-node="${_esc(node.id)}" data-param="${_esc(p.name)}">literal</div>
            <div class="insp-source-tab${isState?' active':''}" data-act="src" data-src="state" data-node="${_esc(node.id)}" data-param="${_esc(p.name)}">from state</div>
          </div>
          ${control}
        </div>`;
      }
      const om = node.output_map || {};
      const omRows = Object.entries(om).map(([k,v],i)=>`<div class="insp-om-row" data-omr="${i}">
          <input data-act="omk" data-node="${_esc(node.id)}" data-i="${i}" value="${_esc(k)}" placeholder="state_key">
          <span style="color:var(--dim,#7a8290)">←</span>
          <input data-act="omv" data-node="${_esc(node.id)}" data-i="${i}" value="${_esc(v||'')}" placeholder="result.field">
          <button class="btn xs danger" data-act="omdel" data-node="${_esc(node.id)}" data-i="${i}">×</button>
        </div>`).join('');
      const condOpts = ['<option value="">always</option>'].concat(refKeys.map(k=>`<option value="${_esc(k)}"${node.condition===k?' selected':''}>${_esc(k)}</option>`)).join('');
      return `
        <div class="insp-section">
          <div class="insp-section-h">Inputs — data wiring${inputs.length?` (${inputs.filter(p=>p.required).length} req)`:''}</div>
          ${rows || '<div class="empty">No declared inputs.</div>'}
        </div>
        <div class="insp-section">
          <div class="insp-section-h">Output key <span class="pdesc">state[key] = result</span></div>
          <div class="insp-row"><input data-act="out" data-node="${_esc(node.id)}" value="${_esc(node.out||'')}" placeholder="state key for this node's result"></div>
        </div>
        <div class="insp-section">
          <div class="insp-section-h">Output extraction <span class="pdesc">state_key ← result.field</span></div>
          ${omRows}
          <button class="btn xs" data-act="omadd" data-node="${_esc(node.id)}">+ extract field</button>
        </div>
        <div class="insp-section">
          <div class="insp-section-h">Run condition</div>
          <div class="insp-row"><select data-act="cond" data-node="${_esc(node.id)}">${condOpts}</select>
            <div class="pdesc" style="margin-top:2px">runs only if the chosen state key is truthy</div></div>
        </div>`;
    }
    _actionsHtml(node){
      return `<div class="insp-actions">
        <button class="btn sm" data-act="dup" data-node="${_esc(node.id)}">Duplicate</button>
        <button class="btn sm danger" data-act="del" data-node="${_esc(node.id)}">Delete</button>
      </div>`;
    }
    _defaultInspectorHtml(node){
      const sc = this._schema(node) || {};
      const longBadge = sc.long_running ? '<span class="long-pill">long</span>' : '';
      return `<div class="insp-h">${_esc(node.type)}${longBadge}</div>
        ${sc.description?`<div class="insp-desc">${_esc(sc.description)}</div>`:''}
        ${this._wiringHtml(node)}
        ${this._actionsHtml(node)}`;
    }
  }

  customElements.define('vera-flow-builder', VeraFlowBuilder);
})();
