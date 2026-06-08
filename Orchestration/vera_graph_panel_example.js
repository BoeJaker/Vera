/**
 * vera_graph_panel_example.js — reference companion sidebar panel
 * ============================================================================
 * Demonstrates the modular sidebar plugin contract for vera_graph.js.
 *
 * Load this file AFTER vera_graph.js on any page that embeds the graph:
 *
 *   <script src="/static/vera_graph.js"></script>
 *   <script src="/static/vera_graph_panel_example.js"></script>
 *
 * Every graph created via veraUI.Graph.create() — anywhere on the page, now or
 * later — automatically gains a tab in its left rail for this panel. No changes
 * to the host page or to vera_graph.js are required.
 *
 * The real Loom / Discover / Table panels follow this exact shape.
 * ----------------------------------------------------------------------------
 * Plugin contract (passed to veraUI.Graph.registerPanel):
 *
 *   id      : unique string key
 *   title   : header / tooltip label
 *   icon    : single glyph for the rail tab
 *   order   : sort position in the rail (lower = higher up)
 *   mount   : function(bodyEl, graph, api)  — build the panel UI
 *   unmount : function(bodyEl, graph)        — optional cleanup
 *
 * `graph` is the live graph instance — call graph.load({nodes,edges}),
 * graph.fetchSnapshot(layer, params), graph.focusNode(id), graph.openPanel(id),
 * graph.closePanel(), etc.
 *
 * `api` provides: { activate(), isActive(), graphContainer, apiBase, eventBus }.
 */
(function(){
  'use strict';

  if (!window.veraUI || !window.veraUI.Graph || !window.veraUI.Graph.registerPanel) {
    if (typeof console !== 'undefined') {
      console.warn('vera_graph_panel_example: veraUI.Graph.registerPanel not found — ' +
                   'load vera_graph.js before this file.');
    }
    return;
  }

  window.veraUI.Graph.registerPanel({
    id:    'example',
    title: 'Example',
    icon:  '\u2756',          // ❖
    order: 90,
    mount: function(bodyEl, graph, api){
      // Build whatever UI you like inside bodyEl. Use the Vera CSS variables so
      // it matches the host theme (--bg0/1/2, --acc, --acc2, --text, --dim,
      // --border, --mono).
      bodyEl.innerHTML =
        '<div style="font-size:9px;color:var(--dim,#6a6058);line-height:1.6;margin-bottom:8px">' +
          'Reference panel. Demonstrates the sidebar plugin contract.' +
        '</div>' +
        '<button class="ex-load" style="width:100%;font-size:9px;padding:5px;' +
          'background:rgba(90,158,143,.12);border:1px solid var(--acc,#5a9e8f);' +
          'color:var(--acc,#5a9e8f);border-radius:3px;cursor:pointer;' +
          'font-family:var(--mono,monospace);margin-bottom:6px">Load demo graph</button>' +
        '<div class="ex-stat" style="font-size:8.5px;color:var(--dim,#6a6058);' +
          'font-family:var(--mono,monospace)"></div>';

      var stat = bodyEl.querySelector('.ex-stat');

      bodyEl.querySelector('.ex-load').onclick = function(){
        // Build a small hub-and-spoke graph to exercise the layout.
        var nodes = [
          { id: 'hub-1', name: 'Hub A', type: 'Dataset' },
          { id: 'hub-2', name: 'Hub B', type: 'Dataset' },
        ];
        var edges = [];
        for (var i = 0; i < 40; i++) {
          var id = 'leaf-' + i;
          nodes.push({ id: id, name: 'Record ' + i, type: 'FabricRecord' });
          edges.push({ from: 'hub-1', to: id, rel: 'CONTAINS' });
          if (i % 3 === 0) edges.push({ from: 'hub-2', to: id, rel: 'CONTAINS' });
        }
        graph.load({ nodes: nodes, edges: edges });
        if (stat) stat.textContent = nodes.length + ' nodes, ' + edges.length + ' edges loaded';
      };

      // Optional: react to live graph events via the shared bus.
      if (api.eventBus && api.eventBus.on) {
        this._unsub = api.eventBus.on('*', function(){ /* observe events */ });
      }
    },
    unmount: function(bodyEl, graph){
      if (this._unsub) { try { this._unsub(); } catch(e){} }
    },
  });
})();