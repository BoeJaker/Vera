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
 *   resize     "off" to leave out the drag grip (a host that sizes the embed itself)
 *   full-url   where the affordance points (default /fabric/panel#graph)
 *
 * Events
 *   vera-graph-node   {detail:{node}}  a node was clicked — the host (canvas)
 *                     uses this to scroll source to node.props.start_line
 *
 * THE STRUCTURED RENDERER — renderer="struct"
 * -------------------------------------------
 * The physics graph is the right glance at a stored graph. It is the WRONG
 * picture of one record, one passage or one file: that wants the structured
 * diagram (<vera-structgraph>, /ui/structgraph.js — bands, columns, cards and
 * routed runs; EXPLODE.md §5). With renderer="struct" the embed hosts that
 * element, bare, fed by the Explode contract from one of:
 *
 *   <vera-graph-embed renderer="struct" record="rec-4f2a" ranges="[[0,1180]]" mode="position">
 *   <vera-graph-embed renderer="struct" text="…a pasted passage…">
 *   <vera-graph-embed renderer="struct" src="/some/contract.json">
 *   <vera-graph-embed renderer="struct" path="vera/research/explode_capabilities.py" depth="1">   a repo file (+ its imports)
 *   <vera-graph-embed renderer="struct" path="vera/research/assess_core.py" flow="readability">   ONE function, in source order
 *   <vera-graph-embed renderer="struct" records="rec-1,rec-2,rec-3">                              several records, as lanes
 *   <vera-graph-embed renderer="struct" code="def f(): …" lang="python">                           a snippet (an LLM's, a page's)
 *   el.setDoc(contract)                      a contract the host already has
 *
 *   layers      comma-separated layer ids to run (default: the layers on by default)
 *
 * A card click is relayed as vera-graph-node {node:{id, span, card}} so a host
 * that already listens for the physics graph's clicks hears the same event.
 */
(function () {
  'use strict';

  var GRAPH_JS = '/ui/vera-graph.js';
  var STRUCT_JS = '/ui/structgraph.js';
  var _loading = null;
  var _loadingStruct = null;

  function ensureStructLib() {
    if (window.customElements && window.customElements.get('vera-structgraph')) return Promise.resolve();
    if (_loadingStruct) return _loadingStruct;
    _loadingStruct = new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = STRUCT_JS;
      s.onload = function () { resolve(); };
      s.onerror = function () { reject(new Error('could not load ' + STRUCT_JS)); };
      document.head.appendChild(s);
    });
    return _loadingStruct;
  }

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
      this._h = height;
      if ((this.getAttribute('renderer') || '') === 'struct' && this.getAttribute('resize') !== 'off') this._mkGrip();

      this._note = document.createElement('div'); this._note.className = 'vge-note';   /* a host can quiet it (the canvas's minimal cards) */
      this._note.style.cssText =
        'font-size:8.5px;color:var(--dim,#6a6058);font-family:var(--mono,monospace);' +
        'margin-top:3px;display:flex;justify-content:space-between;align-items:center;gap:8px';
      this.appendChild(this._note);

      var run = (this.getAttribute('renderer') || '') === 'struct' ? this._renderStruct(height) : this._render(height);
      run.catch(function (e) {
        this._fail(String(e && e.message || e));
      }.bind(this));
    }

    // ── a graph the host already has ──────────────────────────────────────
    // setGraph({nodes, edges}) draws nodes and edges the host built (the canvas's fabric results: the query, the
    // datasets that hold them, the records, the tags they share) through the same renderer, instead of a snapshot
    // fetched by layer. Called before the element is connected, it is kept and drawn once the renderer is ready;
    // called again, the graph is reloaded (positions of nodes that persist are kept by load()).
    setGraph(data) {
      this._data = data || { nodes: [], edges: [] };
      if (this._graph) { try { this._graph.load(this._data); } catch (e) {} this._captionData(); }
      return this;
    }

    _captionData() {
      if (!this._note) return;
      this._note.textContent = '';
      var d = this._data || {};
      var left = document.createElement('span');
      left.textContent = (d.caption ? d.caption + ' — ' : '') + ((d.nodes || []).length) + ' nodes, ' + ((d.edges || []).length) + ' edges';
      this._note.appendChild(left);
    }

    // ── the structured renderer ───────────────────────────────────────────
    setDoc(doc) {
      this._doc = doc;
      if (this._struct) { this._struct.setDoc(doc); this._captionStruct(doc); }
      return this;
    }

    async _renderStruct(height) {
      await ensureStructLib();
      var self = this;
      this._host.innerHTML = '';
      this._struct = document.createElement('vera-structgraph');
      this._struct.setAttribute('bare', '');
      var mode = this.getAttribute('mode'); if (mode) this._struct.setAttribute('mode', mode);
      this._struct.style.cssText = 'display:flex;width:100%;height:' + height + 'px';
      this._host.appendChild(this._struct);
      this._struct.addEventListener('vera-explode-select', function (ev) {
        var d = ev.detail || {};
        self.dispatchEvent(new CustomEvent('vera-graph-node', {
          detail: { node: { id: d.id, span: d.span, card: d.card } }, bubbles: true, composed: true,
        }));
      });
      if (this._doc) { this._struct.setDoc(this._doc); this._captionStruct(this._doc); return; }
      var doc = await this._fetchContract();
      if (!doc) return;
      this._doc = doc;
      this._struct.setDoc(doc);
      this._captionStruct(doc);
    }

    async _fetchContract() {
      var base = window._veraBase || '';
      var src = this.getAttribute('src');
      var res, doc;
      try {
        if (src) {
          res = await fetch(src);
        } else {
          var body = {};
          var rec = this.getAttribute('record'), text = this.getAttribute('text');
          var code = this.getAttribute('code'), cpath = this.getAttribute('path');
          var endpoint = '/nlp/explode/prose';
          if (code != null || cpath) {   // code: a snippet (code="…" lang="…"), or a repo file / directory (path="…")
            endpoint = '/code/explode';
            if (code) { body.text = code; body.lang = this.getAttribute('lang') || ''; if (cpath) body.path = cpath; }
            else body.path = cpath;
            var depth = this.getAttribute('depth'); if (depth != null) body.depth = parseInt(depth, 10) || 0;
            // flow="<function>": not what calls what across the file, but what ONE function DOES, in source order
            var flow = this.getAttribute('flow'); if (flow) body.flow = flow;
          } else if (rec) body.record_id = rec;
          else if (this.getAttribute('records')) {      // several records, as lanes — an entity's evidence
            body.record_ids = this.getAttribute('records').split(',').map(function (x) { return x.trim(); }).filter(Boolean);
          } else if (text) body.text = text;
          else { this._fail('nothing to explode — give record, text, code, path or src'); return null; }
          var ranges = this.getAttribute('ranges'); if (ranges) { try { body.ranges = JSON.parse(ranges); } catch (e) {} }
          if (this.hasAttribute('assess')) body.assess = this.getAttribute('assess') || true;   // the verdict rail too
          var mode = this.getAttribute('mode'); if (mode && endpoint !== '/code/explode') body.mode = mode;
          var layers = this.getAttribute('layers'); if (layers && endpoint !== '/code/explode') body.layers = layers.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
          res = await fetch(base + endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        }
        doc = await res.json();
      } catch (e) {
        this._fail('explode failed — ' + (e && e.message || e));
        return null;
      }
      if (!doc || doc.error) { this._fail(doc && doc.error ? doc.error : 'empty contract'); return null; }
      return doc;
    }

    _captionStruct(doc) {
      this._note.innerHTML = '';
      var left = document.createElement('span');
      var on = (doc.layers || []).filter(function (l) { return l.on && !l.error; }).map(function (l) { return l.label || l.id; });
      left.textContent = 'explode — ' + ((doc.cards || []).length) + ' cards, ' + ((doc.edges || []).length) + ' runs' + (on.length ? ' · ' + on.join(' · ') : '');
      // the caption WRAPS rather than clips (owner, 2026-10-03: "the text along the bottom of exploded code is cut off")
      left.style.cssText = 'min-width:0;overflow-wrap:anywhere';
      this._note.appendChild(left);
      var right = document.createElement('span');
      right.style.cssText = 'display:flex;gap:10px;align-items:center;flex:0 0 auto';
      var big = document.createElement('a');
      big.href = '#'; big.className = 'vge-big';
      big.textContent = this._ov ? 'back to size ✕' : 'enlarge ⤢';
      big.title = this._ov ? 'Put the diagram back where it was (Esc)' : 'Open the diagram over the whole window';
      big.style.cssText = 'color:var(--acc,#5a9e8f);text-decoration:none;white-space:nowrap;cursor:pointer';
      big.onclick = function (ev) { ev.preventDefault(); this._enlarge(!this._ov); }.bind(this);
      right.appendChild(big);
      if ((this.getAttribute('expand') || '') !== 'off') {
        var a = document.createElement('a');
        a.href = this.getAttribute('full-url') || '/fabric/panel#graph';
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = 'open in the inspector ↗';
        a.style.cssText = 'color:var(--acc,#5a9e8f);text-decoration:none;white-space:nowrap';
        right.appendChild(a);
      }
      this._note.appendChild(right);
    }

    /* THE DIAGRAM'S SIZE IS THE READER'S (owner, 2026-10-03: "when code is exploded in chat there should be a way to
       increase the size of the box for viewing the exploded code"). Two ways, wherever the embed is hosted: drag the
       grip under the diagram for a taller box, or "enlarge" for the whole window (Esc, the backdrop or "back to size"
       puts it back). The structured renderer refits itself when its box changes (its ResizeObserver). */
    _setHeight(h) {
      /* min-h: a host whose box is smaller (a desk card) says how small; the box is never taller than what holds it */
      var mn = parseInt(this.getAttribute('min-h'), 10); if (!(mn >= 40)) mn = 160;
      h = Math.max(mn, Math.min(Math.round(h), Math.round((window.innerHeight || 900) * 0.92)));
      this._h = h; this._host.style.height = h + 'px';
      if (this._struct) this._struct.style.height = h + 'px';
    }

    _mkGrip() {
      var g = document.createElement('div');
      g.className = 'vge-grip'; g.title = 'Drag to make the diagram taller or shorter';
      g.style.cssText = 'height:9px;cursor:ns-resize;display:flex;align-items:center;justify-content:center;touch-action:none';
      g.innerHTML = '<i style="display:block;width:34px;height:3px;border-radius:2px;background:var(--border,#3a352f)"></i>';
      var self = this, y0 = 0, h0 = 0;
      g.addEventListener('pointerdown', function (ev) {
        if (self._ov) return;
        ev.preventDefault(); y0 = ev.clientY; h0 = self._host.offsetHeight || self._h;
        try { g.setPointerCapture(ev.pointerId); } catch (e) {}
        var move = function (e) { self._setHeight(h0 + e.clientY - y0); };
        var up = function () {
          g.removeEventListener('pointermove', move); g.removeEventListener('pointerup', up); g.removeEventListener('pointercancel', up);
          if (self._struct && self._struct.fit) { try { self._struct.fit(); } catch (e) {} }
        };
        g.addEventListener('pointermove', move); g.addEventListener('pointerup', up); g.addEventListener('pointercancel', up);
      });
      this._grip = g;
      this.appendChild(g);
    }

    _enlarge(on) {
      if (!!on === !!this._ov) return;
      var self = this;
      if (on) {
        var back = document.createElement('div');
        back.style.cssText = 'position:fixed;inset:0;z-index:2147482990;background:rgba(0,0,0,.5)';
        back.onclick = function () { self._enlarge(false); };
        var ov = document.createElement('div');
        ov.className = 'vge-ov';
        ov.style.cssText = 'position:fixed;inset:3vh 3vw;z-index:2147482991;display:flex;flex-direction:column;padding:8px;' +
          'background:var(--bg0,#12100e);border:1px solid var(--border,#2a2622);border-radius:6px;box-shadow:0 18px 60px rgba(0,0,0,.55)';
        // the embed goes up to the page and a placeholder of its size holds its place, so the reply does not jump
        this._ph = document.createElement('div');
        this._ph.style.height = this.offsetHeight + 'px';
        this.parentNode.insertBefore(this._ph, this);
        this._css = this.style.cssText;
        this._ov = ov; this._back = back;
        document.body.appendChild(back); document.body.appendChild(ov); ov.appendChild(this);
        this.style.cssText = 'display:flex;flex-direction:column;position:relative;flex:1 1 auto;min-height:0;width:100%';
        this._host.style.height = ''; this._host.style.flex = '1 1 auto'; this._host.style.minHeight = '0';
        if (this._struct) this._struct.style.height = '100%';
        if (this._grip) this._grip.style.display = 'none';
        this._esc = function (ev) { if (ev.key === 'Escape') { ev.stopPropagation(); self._enlarge(false); } };
        document.addEventListener('keydown', this._esc, true);
      } else {
        document.removeEventListener('keydown', this._esc, true);
        if (this._ph && this._ph.parentNode) { this._ph.parentNode.insertBefore(this, this._ph); this._ph.remove(); }
        try { this._ov.remove(); this._back.remove(); } catch (e) {}
        this._ov = null; this._back = null; this._ph = null;
        this.style.cssText = this._css || 'display:block;width:100%';
        this._host.style.flex = ''; this._host.style.minHeight = '';
        if (this._grip) this._grip.style.display = 'flex';
        this._setHeight(this._h);
      }
      if (this._doc) this._captionStruct(this._doc);
      if (this._struct && this._struct.fit) { try { this._struct.fit(); } catch (e) {} }
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
        calm: this.hasAttribute('calm'),   /* settled before it is shown, still after: a graph among other things */
        /* A GRAPH OF THE HOST'S OWN DATA TAKES NO ESTATE EVENTS (owner, 2026-10-11: the desk's graph "flickers like a broken
           lightbulb"): every graph subscribed to the fabric's live events (record ingested, loom, acquire progress) and
           each one ADDED a node to it and pulsed it - on a busy estate a stream of foreign nodes into a desk card's graph of
           a search, re-laid out each time. A graph the host draws from its own data (renderer=data, setGraph), or a calm one,
           shows that data only. */
        subscribeLiveEvents: !(this.getAttribute('renderer') === 'data' || this.hasAttribute('calm')),
        defaultLayer: layer,
        layerOpts: params,
        onNodeClick: function (node) {
          // With `details` (the desk, owner 2026-10-17: "i need to be able to click nodes to see details etc - vera graph
          // js already has all of this its just not usable from the desk") a click opens vera_graph's own detail drawer and
          // the host hears it as `vera-graph-detail`; a double click is the host's action (`vera-graph-node`).
          if (self.hasAttribute('details')) {
            self.dispatchEvent(new CustomEvent('vera-graph-detail', { detail: { node: node }, bubbles: true, composed: true }));
            return undefined;
          }
          self.dispatchEvent(new CustomEvent('vera-graph-node', {
            detail: { node: node },
            bubbles: true,
            composed: true,
          }));
          // Returning false leaves the detail drawer closed — inline graphs are
          // a glance, not a workbench. The full view is one click away.
          return false;
        },
        onNodeDblClick: function (node) {
          if (!self.hasAttribute('details')) return undefined;   /* unchanged without it: vera_graph's own (expand / collapse) */
          self.dispatchEvent(new CustomEvent('vera-graph-node', { detail: { node: node }, bubbles: true, composed: true }));
          return false;
        },
      });

      // a host that handed its own nodes and edges (setGraph, or renderer="data" with setGraph to follow) draws those
      if (this._data || (this.getAttribute('renderer') || '') === 'data') {
        if (this._data) { try { this._graph.load(this._data); } catch (e) { this._fail('load failed'); return; } }
        this._captionData();
        return;
      }
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
      if (this._ov) return;   // only moving up to (or back from) the enlarged view
      try { if (this._graph && this._graph.destroy) this._graph.destroy(); } catch (e) {}
    }
  }

  if (!customElements.get('vera-graph-embed')) {
    customElements.define('vera-graph-embed', VeraGraphEmbed);
  }
})();
