/**
 * vera_graph_panel_explode.js — "Explode" sidebar panel for vera_graph.js
 * ============================================================================
 * Turns a body of prose, or a stored fabric graph, or (when it exists) a code
 * graph, into a relational graph in the current viewer.
 *
 * Load AFTER vera_graph.js:
 *   <script src="/ui/vera-graph.js"></script>
 *   <script src="/ui/vera-graph-panel-explode.js"></script>
 *
 * WHY THIS IS ONE PANEL AND NOT THREE
 * -----------------------------------
 * vera_graph.js's fetchSnapshot() already routes by LAYER:
 *
 *   'memory'                -> the memory graph
 *   'entity'                -> /fabric/entity_graph/snapshot      (prose entities)
 *   anything else           -> /fabric/graphs/snapshot?graph=<layer>
 *
 * That last line is the important one: ANY registered fabric graph is already a
 * valid layer. So a code graph registered as `fabric.graphs.register(name="code")`
 * renders here with **no change to vera_graph.js and no change to this panel** —
 * it simply appears in the source list. Prose and code are the same operation
 * against different layers, which is why they share one panel.
 *
 * Sources
 *   • Entities      the prose entity graph (persisted)
 *   • <fabric graph>  any registered graph, listed live from /fabric/graphs
 *   • Text          paste prose, extract entities+relations WITHOUT persisting
 *                   (POST /fabric/entity_graph/extract_text) — the "explode
 *                   this passage" case, useful before committing anything
 *
 * Contract used (all pre-existing):
 *   GET  /fabric/graphs
 *   GET  /fabric/entity_graph/types
 *   POST /fabric/entity_graph/extract_text   {items:[{id,text}], content_type}
 *   graph.fetchSnapshot(layer, params) / graph.load({nodes,edges})
 */
(function () {
  'use strict';

  if (!window.veraUI || !window.veraUI.Graph || !window.veraUI.Graph.registerPanel) {
    if (typeof console !== 'undefined') {
      console.warn('vera_graph_panel_explode: veraUI.Graph.registerPanel not found — ' +
                   'load vera_graph.js before this file.');
    }
    return;
  }

  var CSS_BTN =
    'width:100%;font-size:9px;padding:5px;background:rgba(90,158,143,.12);' +
    'border:1px solid var(--acc,#5a9e8f);color:var(--acc,#5a9e8f);border-radius:3px;' +
    'cursor:pointer;font-family:var(--mono,monospace)';
  var CSS_FIELD =
    'width:100%;font-size:9px;padding:4px;background:var(--bg0,#12100e);' +
    'border:1px solid var(--border,#2a2622);color:var(--text,#d8d0c6);' +
    'border-radius:3px;font-family:var(--mono,monospace);box-sizing:border-box';
  var CSS_LABEL =
    'font-size:8.5px;color:var(--dim,#6a6058);text-transform:uppercase;' +
    'letter-spacing:.06em;margin:8px 0 3px';

  function el(html) {
    var d = document.createElement('div');
    d.innerHTML = html;
    return d.firstElementChild;
  }

  window.veraUI.Graph.registerPanel({
    id: 'explode',
    title: 'Explode',
    icon: '✵',
    order: 15,

    mount: function (bodyEl, graph, api) {
      var self = this;
      var base = (api && api.apiBase) || '';

      bodyEl.innerHTML =
        '<div style="font-size:9px;color:var(--dim,#6a6058);line-height:1.5;margin-bottom:6px">' +
          'Render a source as a relational graph.' +
        '</div>' +
        '<div style="' + CSS_LABEL + '">Source</div>' +
        '<select class="xp-src" style="' + CSS_FIELD + '">' +
          '<option value="entity">Entities (prose)</option>' +
          '<option value="__text">Text — extract live</option>' +
        '</select>' +
        '<div class="xp-scope">' +
          '<div style="' + CSS_LABEL + '">Scope (optional)</div>' +
          '<input class="xp-dataset" placeholder="dataset_id" style="' + CSS_FIELD + '">' +
          '<div style="' + CSS_LABEL + '">Entity type</div>' +
          '<select class="xp-type" style="' + CSS_FIELD + '"><option value="">any</option></select>' +
        '</div>' +
        '<div class="xp-textwrap" style="display:none">' +
          '<div style="' + CSS_LABEL + '">Text</div>' +
          '<textarea class="xp-text" rows="7" placeholder="Paste prose to explode into entities and relations. Nothing is persisted." ' +
            'style="' + CSS_FIELD + ';resize:vertical"></textarea>' +
        '</div>' +
        '<div style="' + CSS_LABEL + '">Limit</div>' +
        '<input class="xp-limit" type="number" value="200" min="10" max="2000" style="' + CSS_FIELD + '">' +
        '<div style="margin-top:8px"><button class="xp-go" style="' + CSS_BTN + '">Explode</button></div>' +
        '<div class="xp-stat" style="font-size:8.5px;color:var(--dim,#6a6058);' +
          'font-family:var(--mono,monospace);margin-top:7px;line-height:1.5"></div>';

      var srcSel = bodyEl.querySelector('.xp-src');
      var typeSel = bodyEl.querySelector('.xp-type');
      var dsIn = bodyEl.querySelector('.xp-dataset');
      var limIn = bodyEl.querySelector('.xp-limit');
      var textWrap = bodyEl.querySelector('.xp-textwrap');
      var scopeWrap = bodyEl.querySelector('.xp-scope');
      var textIn = bodyEl.querySelector('.xp-text');
      var stat = bodyEl.querySelector('.xp-stat');

      function say(msg, bad) {
        if (!stat) return;
        stat.textContent = msg || '';
        stat.style.color = bad ? 'var(--err,#b4563c)' : 'var(--dim,#6a6058)';
      }

      // ── Populate the source list from the live graph registry ──────────────
      // Any registered fabric graph is a valid layer, so a code graph shows up
      // here the moment it is registered — no change to this file.
      fetch(base + '/fabric/graphs')
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var graphs = (d && (d.graphs || d.registered)) || [];
          graphs.forEach(function (g) {
            var name = (typeof g === 'string') ? g : (g.name || g.graph);
            if (!name || name === 'entity') return;
            var o = document.createElement('option');
            o.value = name;
            o.textContent = name + (g && g.description ? ' — ' + g.description : '');
            srcSel.insertBefore(o, srcSel.lastElementChild);
          });
        })
        .catch(function () { /* registry unavailable: the two built-ins still work */ });

      fetch(base + '/fabric/entity_graph/types')
        .then(function (r) { return r.json(); })
        .then(function (d) {
          ((d && d.types) || []).slice(0, 40).forEach(function (t) {
            var o = document.createElement('option');
            o.value = t.type;
            o.textContent = t.type + ' (' + t.count + ')';
            typeSel.appendChild(o);
          });
        })
        .catch(function () {});

      function syncMode() {
        var isText = srcSel.value === '__text';
        textWrap.style.display = isText ? '' : 'none';
        scopeWrap.style.display = isText ? 'none' : '';
        typeSel.parentElement.style.display =
          (srcSel.value === 'entity') ? '' : 'none';
      }
      srcSel.onchange = syncMode;
      syncMode();

      // ── Explode ───────────────────────────────────────────────────────────
      bodyEl.querySelector('.xp-go').onclick = async function () {
        var limit = parseInt(limIn.value, 10) || 200;

        if (srcSel.value === '__text') {
          var text = (textIn.value || '').trim();
          if (!text) { say('nothing to extract', true); return; }
          say('extracting…');
          try {
            var res = await fetch(base + '/fabric/entity_graph/extract_text', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                items: [{ id: 'pasted', text: text }],
                content_type: 'text',
              }),
            });
            var data = await res.json();
            var nodes = (data && data.nodes) || [];
            var edges = (data && data.edges) || [];
            if (!nodes.length) {
              say('no entities found in that text', true);
              return;
            }
            graph.load({ nodes: nodes, edges: edges });
            say(nodes.length + ' entities, ' + edges.length + ' relations — not persisted');
          } catch (e) {
            say('extract failed: ' + e, true);
          }
          return;
        }

        // A stored layer: 'entity' or any registered fabric graph.
        var params = { limit: limit };
        if (dsIn.value.trim()) params.dataset_id = dsIn.value.trim();
        if (srcSel.value === 'entity' && typeSel.value) params.entity_type = typeSel.value;
        say('loading ' + srcSel.value + '…');
        try {
          await graph.fetchSnapshot(srcSel.value, params);
          say('loaded ' + srcSel.value);
        } catch (e) {
          say('load failed: ' + e, true);
        }
      };
    },

    unmount: function (bodyEl) {
      if (bodyEl) bodyEl.innerHTML = '';
    },
  });
})();
