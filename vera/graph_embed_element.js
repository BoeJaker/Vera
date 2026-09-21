/**
 * graph_embed_element.js — <vera-graph-embed>, the bare graph for chat
 * ============================================================================
 * A graph with NO chrome: no search box, no legend, no left rail, no layer
 * switcher, no node-action menu. Just nodes and edges, sized to sit inline in a
 * chat turn or a canvas block.
 *
 *   <vera-graph-embed layer="entity" limit="60" height="240"></vera-graph-embed>
 *   <vera-graph-embed layer="code" params='{"root":"vera/agents/agents.py"}'></vera-graph-embed>
 *
 * WHY THIS DOES NOT FORK vera_graph.js
 * ------------------------------------
 * vera_graph.js already takes every switch this needs — showSearch, showLegend,
 * showLeftPanel, layerUI, actionsEnabled, memoryLayer, height. So the bare view
 * is a CONFIGURATION of the same renderer, not a second implementation. That
 * matters: a fork would drift, and the full view and the inline view would
 * slowly stop agreeing about what a graph looks like.
 *
 * `layer` is passed straight through to fetchSnapshot, which routes:
 *   'memory' -> the memory graph
 *   'entity' -> /fabric/entity_graph/snapshot   (prose)
 *   else     -> /fabric/graphs/snapshot?graph=<layer>
 * so a code graph registered under fabric.graphs works here the day it exists,
 * with no change to this file.
 *
 * Attributes
 *   layer      snapshot layer (default 'entity')
 *   params     JSON object passed to fetchSnapshot (dataset_id, entity_type, root…)
 *   limit      shorthand for params.limit (default 60 — inline, not exhaustive)
 *   height     px (default 240)
 *   expand     "off" to hide the open-in-full-view affordance
 *   full-url   where the affordance points (default /fabric/panel#graph)
 *
 * Events
 *   vera-graph-node   {detail:{node}}  a node was clicked — the host (canvas)
 *                     uses this to scroll source to node.props.start_line
 */
(function () {
  'use strict';

  var GRAPH_JS = '/ui/vera-graph.js';
  var _loading = null;

  function ensureGraphLib() {
    if (window.veraUI && window.veraUI.Graph) return Promise.resolve();
    if (_loading) return _loading;
    _loading = new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = GRAPH_JS;
      s.onload = function () { resolve(); };
      s.onerror = function () { reject(new Error('could not load ' + GRAPH_JS)); };
      document.head.appendChild(s);
    });
    return _loading;
  }

  function parseParams(raw) {
    if (!raw) return {};
    try { return JSON.parse(raw); } catch (e) { return {}; }
  }

  class VeraGraphEmbed extends HTMLElement {
    connectedCallback() {
      if (this._mounted) return;
      this._mounted = true;

      var height = parseInt(this.getAttribute('height'), 10) || 240;
      this.style.display = 'block';
      this.style.position = 'relative';

      this._host = document.createElement('div');
      this._host.style.cssText =
        'width:100%;height:' + height + 'px;border:1px solid var(--border,#2a2622);' +
        'border-radius:var(--radius,4px);overflow:hidden;background:var(--bg0,#12100e)';
      this.appendChild(this._host);

      this._note = document.createElement('div');
      this._note.style.cssText =
        'font-size:8.5px;color:var(--dim,#6a6058);font-family:var(--mono,monospace);' +
        'margin-top:3px;display:flex;justify-content:space-between;align-items:center;gap:8px';
      this.appendChild(this._note);

      this._render(height).catch(function (e) {
        this._fail(String(e && e.message || e));
      }.bind(this));
    }

    _fail(msg) {
      // Say what went wrong in place. A blank rectangle in a chat turn is the
      // worst outcome: the reader cannot tell "no data" from "broken".
      this._host.innerHTML =
        '<div style="padding:10px;font-size:9px;color:var(--err,#b4563c);' +
        'font-family:var(--mono,monospace)">graph unavailable — ' + msg + '</div>';
    }

    async _render(height) {
      await ensureGraphLib();

      var layer = this.getAttribute('layer') || 'entity';
      var params = parseParams(this.getAttribute('params'));
      if (!params.limit) {
        params.limit = parseInt(this.getAttribute('limit'), 10) || 60;
      }

      var self = this;
      // Every piece of chrome off. These are vera_graph.js's own options, so
      // the inline view and the full view stay one renderer.
      this._graph = window.veraUI.Graph.create(this._host, {
        height: height,
        showSearch: false,
        showLegend: false,
        showLeftPanel: false,
        showLayerToggle: false,
        layerUI: false,
        actionsEnabled: false,
        memoryLayer: false,
        memoryStore: false,
        defaultLayer: layer,
        layerOpts: params,
        onNodeClick: function (node) {
          self.dispatchEvent(new CustomEvent('vera-graph-node', {
            detail: { node: node },
            bubbles: true,
            composed: true,
          }));
          // Returning false leaves the detail drawer closed — inline graphs are
          // a glance, not a workbench. The full view is one click away.
          return false;
        },
      });

      try {
        await this._graph.fetchSnapshot(layer, params);
      } catch (e) {
        this._fail('snapshot failed');
        return;
      }
      this._caption(layer, params);
    }

    _caption(layer, params) {
      var n = 0, e = 0;
      try {
        var g = this._graph;
        n = (g && g.nodes && g.nodes().length) || 0;
        e = (g && g.edges && g.edges().length) || 0;
      } catch (_) {}
      var left = document.createElement('span');
      left.textContent = layer + (n ? ' — ' + n + ' nodes, ' + e + ' edges' : '');
      this._note.appendChild(left);

      if ((this.getAttribute('expand') || '') !== 'off') {
        var a = document.createElement('a');
        a.href = this.getAttribute('full-url') || '/fabric/panel#graph';
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = 'open full graph ↗';
        a.style.cssText = 'color:var(--acc,#5a9e8f);text-decoration:none;white-space:nowrap';
        this._note.appendChild(a);
      }
    }

    disconnectedCallback() {
      try { if (this._graph && this._graph.destroy) this._graph.destroy(); } catch (e) {}
    }
  }

  if (!customElements.get('vera-graph-embed')) {
    customElements.define('vera-graph-embed', VeraGraphEmbed);
  }
})();
