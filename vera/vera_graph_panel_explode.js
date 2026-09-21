/**
 * vera_graph_panel_explode.js — "Explode" sidebar panel for vera_graph.js
 * ============================================================================
 * Inspect ONE thing as structure: a fabric record, a slice of one (character
 * ranges), a few records side by side, or a pasted passage — as the structured
 * diagram (<vera-structgraph>, /ui/structgraph.js): paragraphs as plates,
 * entities as cards standing where they were first mentioned, relations as
 * routed runs styled by kind and by how sure the extractor was. Nothing is
 * persisted. (Loom is the other tool: it stitches relations INTO the fabric.)
 *
 * Load AFTER vera_graph.js:
 *   <script src="/ui/vera-graph.js"></script>
 *   <script src="/ui/vera-graph-panel-explode.js"></script>
 *
 * WHY THIS IS NOT THE PHYSICS GRAPH
 * ---------------------------------
 * A force-directed graph is a fine glance at a stored graph. It is the wrong
 * picture of one record: it does not read. The structured renderer lays the
 * record out as bands × columns with orthogonal runs — an architecture diagram
 * of the text — and every card carries the SPAN it came from, so a click shows
 * the passage it stands for. The diagram is drawn OVER the graph stage
 * (panelApi.graphContainer) and stepped out of with one click.
 *
 * Contract used
 *   GET  /nlp/explode/layers                 the analysis layers and their defaults
 *   POST /nlp/explode/prose                  {text | record_id | record_ids, ranges, mode, layers}
 *                                            → the Explode contract (EXPLODE.md §3)
 *   graph.state.selected                     the record a click on the graph selected
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

  var STRUCT_JS = '/ui/structgraph.js';
  var CSS_BTN =
    'width:100%;font-size:9px;padding:5px;background:rgba(90,158,143,.12);' +
    'border:1px solid var(--acc,#5a9e8f);color:var(--acc,#5a9e8f);border-radius:3px;' +
    'cursor:pointer;font-family:var(--mono,monospace)';
  var CSS_BTN2 =
    'font-size:9px;padding:3px 7px;background:none;border:1px solid var(--border,#2a2622);' +
    'color:var(--dim,#6a6058);border-radius:3px;cursor:pointer;font-family:var(--mono,monospace)';
  var CSS_FIELD =
    'width:100%;font-size:9px;padding:4px;background:var(--bg0,#12100e);' +
    'border:1px solid var(--border,#2a2622);color:var(--text,#d8d0c6);' +
    'border-radius:3px;font-family:var(--mono,monospace);box-sizing:border-box';
  var CSS_LABEL =
    'font-size:8.5px;color:var(--dim,#6a6058);text-transform:uppercase;' +
    'letter-spacing:.06em;margin:8px 0 3px';

  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

  var _structLoading = null;
  function ensureStruct() {
    if (window.customElements && window.customElements.get('vera-structgraph')) return Promise.resolve();
    if (_structLoading) return _structLoading;
    _structLoading = new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = STRUCT_JS;
      s.onload = function () { resolve(); };
      s.onerror = function () { reject(new Error('could not load ' + STRUCT_JS)); };
      document.head.appendChild(s);
    });
    return _structLoading;
  }

  // "12-340, 400-900" → [[12,340],[400,900]]
  function parseRanges(s) {
    var out = [];
    String(s || '').split(/[,;]+/).forEach(function (part) {
      var m = part.trim().match(/^(\d+)\s*[-–:]\s*(\d+)$/);
      if (m) out.push([parseInt(m[1], 10), parseInt(m[2], 10)]);
    });
    return out;
  }

  // the passage a span stands for, with the span itself marked
  function excerpt(doc, span) {
    if (!doc || !span) return '';
    var t = doc.source && doc.source.text;
    if (t && typeof t === 'object') t = t[span.path];
    if (typeof t !== 'string') return '';
    var s = Math.max(0, span.start | 0), e = Math.min(t.length, span.end | 0);
    var a = Math.max(0, s - 90), b = Math.min(t.length, e + 90);
    return (a > 0 ? '…' : '') + esc(t.slice(a, s)) + '<mark style="background:rgba(90,158,143,.35);color:inherit">' + esc(t.slice(s, e)) + '</mark>' + esc(t.slice(e, b)) + (b < t.length ? '…' : '');
  }

  window.veraUI.Graph.registerPanel({
    id: 'explode',
    title: 'Explode',
    icon: '✵',
    order: 15,

    mount: function (bodyEl, graph, api) {
      var base = (api && api.apiBase) || '';
      var stage = (api && api.graphContainer) || (graph && graph.container);

      bodyEl.innerHTML =
        '<div style="font-size:9px;color:var(--dim,#6a6058);line-height:1.5;margin-bottom:6px">' +
          'Inspect one record, a slice of it, a few records, or a passage as a structured diagram. Nothing is persisted.' +
        '</div>' +
        '<div style="' + CSS_LABEL + '">What</div>' +
        '<select class="xp-what" style="' + CSS_FIELD + '">' +
          '<option value="record">A record</option>' +
          '<option value="records">Several records — lanes</option>' +
          '<option value="text">A passage — pasted</option>' +
          '<option value="code">Code — a repo file, directory or snippet</option>' +
        '</select>' +
        '<div class="xp-w-code" style="display:none">' +
          '<div style="' + CSS_LABEL + '">Repo path (file or directory)</div>' +
          '<input class="xp-code-path" placeholder="vera/research/explode_capabilities.py" style="' + CSS_FIELD + '">' +
          '<div style="display:flex;gap:6px;align-items:center;margin-top:4px;font-size:9px;color:var(--dim,#6a6058)">' +
            '<label style="display:flex;gap:4px;align-items:center;cursor:pointer"><input type="checkbox" class="xp-code-hop" checked style="margin:0">pull in the files it imports</label>' +
          '</div>' +
          '<div style="' + CSS_LABEL + '">— or a snippet</div>' +
          '<textarea class="xp-code-text" rows="6" placeholder="Paste code. tree-sitter reads broken or partial code when installed; ast / patterns otherwise." style="' + CSS_FIELD + ';resize:vertical"></textarea>' +
          '<select class="xp-code-lang" style="' + CSS_FIELD + ';margin-top:4px"><option value="">language — detect</option><option value="python">python</option><option value="javascript">javascript</option><option value="typescript">typescript</option><option value="css">css</option><option value="html">html</option></select>' +
        '</div>' +
        '<div class="xp-w-record">' +
          '<div style="' + CSS_LABEL + '">Record id</div>' +
          '<div style="display:flex;gap:4px"><input class="xp-record" placeholder="record id" style="' + CSS_FIELD + '">' +
          '<button class="xp-pick" title="Use the record selected on the graph" style="' + CSS_BTN2 + ';white-space:nowrap">selected</button></div>' +
          '<div style="' + CSS_LABEL + '">Slice (optional)</div>' +
          '<input class="xp-ranges" placeholder="start-end, start-end  (characters)" style="' + CSS_FIELD + '">' +
        '</div>' +
        '<div class="xp-w-records" style="display:none">' +
          '<div style="' + CSS_LABEL + '">Record ids — one per line</div>' +
          '<textarea class="xp-records" rows="4" style="' + CSS_FIELD + ';resize:vertical"></textarea>' +
        '</div>' +
        '<div class="xp-w-text" style="display:none">' +
          '<div style="' + CSS_LABEL + '">Passage</div>' +
          '<textarea class="xp-text" rows="7" placeholder="Paste prose to explode. Nothing is persisted." ' +
            'style="' + CSS_FIELD + ';resize:vertical"></textarea>' +
        '</div>' +
        '<div style="' + CSS_LABEL + '">Mode</div>' +
        '<select class="xp-mode" style="' + CSS_FIELD + '">' +
          '<option value="">auto — position for a slice or passage, type for a record</option>' +
          '<option value="position">position — paragraphs down, entity types across</option>' +
          '<option value="type">type — a band per entity type, paragraphs across</option>' +
        '</select>' +
        '<div style="' + CSS_LABEL + '">Layers</div>' +
        '<div class="xp-layers" style="font-size:9px;line-height:1.7;font-family:var(--mono,monospace)">loading…</div>' +
        '<div style="margin-top:6px;font-size:9px;color:var(--dim,#6a6058)"><label style="display:flex;gap:5px;align-items:center;cursor:pointer" title="Run the scorers too: readability · structure · sources · AI-likelihood (stylometric, low confidence) · trust for prose; complexity · smells · clones · tests · provenance · health for code. Every verdict says what produced it; click one for its evidence.">' +
          '<input type="checkbox" class="xp-assess" checked style="margin:0">assess — a verdict rail with evidence</label></div>' +
        '<div style="margin-top:8px"><button class="xp-go" style="' + CSS_BTN + '">Explode</button></div>' +
        '<div class="xp-stat" style="font-size:8.5px;color:var(--dim,#6a6058);' +
          'font-family:var(--mono,monospace);margin-top:7px;line-height:1.5"></div>';

      var $ = function (sel) { return bodyEl.querySelector(sel); };
      var whatSel = $('.xp-what'), recIn = $('.xp-record'), rangesIn = $('.xp-ranges'), recsIn = $('.xp-records'),
          textIn = $('.xp-text'), modeSel = $('.xp-mode'), layersEl = $('.xp-layers'), stat = $('.xp-stat');

      function say(msg, bad) {
        if (!stat) return;
        stat.innerHTML = msg || '';
        stat.style.color = bad ? 'var(--err,#b4563c)' : 'var(--dim,#6a6058)';
      }
      function syncWhat() {
        var w = whatSel.value;
        $('.xp-w-record').style.display = w === 'record' ? '' : 'none';
        $('.xp-w-records').style.display = w === 'records' ? '' : 'none';
        $('.xp-w-text').style.display = w === 'text' ? '' : 'none';
        $('.xp-w-code').style.display = w === 'code' ? '' : 'none';
        // prose has modes and NLP layers; code has one layout and its own layers (drawn from the reply)
        [modeSel, modeSel.previousElementSibling, layersEl, layersEl.previousElementSibling].forEach(function (el) { if (el) el.style.display = w === 'code' ? 'none' : ''; });
      }
      whatSel.onchange = syncWhat;
      syncWhat();

      // the record selected on the graph, when one is
      function selectedId() {
        var n = graph && graph.state && graph.state.selected;
        if (!n) return '';
        var t = String(n.type || (n.labels && n.labels[0]) || '');
        return (/record/i.test(t) || n.props && n.props.record_id) ? String((n.props && n.props.record_id) || n.id) : String(n.id || '');
      }
      $('.xp-pick').onclick = function () {
        var id = selectedId();
        if (!id) { say('select a record on the graph first', true); return; }
        recIn.value = id; whatSel.value = 'record'; syncWhat(); say('record ' + id);
      };
      if (!recIn.value) recIn.value = selectedId();

      // ── the layers, from the registry ───────────────────────────────────────
      fetch(base + '/nlp/explode/layers')
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var L = (d && d.layers) || [];
          if (!L.length) { layersEl.textContent = 'no layers registered'; return; }
          layersEl.innerHTML = L.map(function (l) {
            return '<label style="display:flex;gap:5px;align-items:center;cursor:pointer" title="' + esc((l.by || '') + (l.where ? ' · ' + l.where : '')) + '">' +
              '<input type="checkbox" data-l="' + esc(l.id) + '"' + (l.default_on ? ' checked' : '') + ' style="margin:0">' +
              '<span>' + esc(l.label || l.id) + '</span><span style="color:var(--dim,#6a6058)">' + esc(l.where || '') + '</span></label>';
          }).join('');
        })
        .catch(function () { layersEl.textContent = 'layers unavailable — the defaults will run'; });

      function chosenLayers() {
        var boxes = layersEl.querySelectorAll('input[data-l]');
        if (!boxes.length) return null;
        var out = [];
        boxes.forEach(function (b) { if (b.checked) out.push(b.dataset.l); });
        return out;
      }

      // ── the diagram, over the stage ─────────────────────────────────────────
      var overlay = null, sg = null, lastDoc = null;
      function closeOverlay() { if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay); overlay = null; sg = null; }
      bodyEl._xpClose = closeOverlay;
      function openOverlay(doc, label) {
        closeOverlay();
        if (!stage) { say('no graph stage to draw on', true); return; }
        if (getComputedStyle(stage).position === 'static') stage.style.position = 'relative';
        overlay = document.createElement('div');
        overlay.className = 'vg-explode-overlay';
        overlay.style.cssText = 'position:absolute;inset:0;z-index:60;display:flex;flex-direction:column;background:var(--bg0,#12100e)';
        overlay.innerHTML =
          '<div style="display:flex;align-items:center;gap:8px;padding:5px 10px;font-size:9px;font-family:var(--mono,monospace);color:var(--dim,#6a6058);border-bottom:1px solid var(--border,#2a2622)">' +
            '<button class="xp-back" style="' + CSS_BTN2 + '">← graph</button>' +
            '<span class="xp-label" style="color:var(--text,#d8d0c6)">' + esc(label) + '</span>' +
            '<span class="xp-counts"></span>' +
            '<span style="flex:1"></span>' +
            '<span class="xp-hint">click a card for its passage · double-click to re-explode from it</span>' +
          '</div>' +
          '<vera-structgraph style="flex:1;min-height:0;display:flex"></vera-structgraph>' +
          '<div class="xp-excerpt" style="display:none;padding:6px 10px;font-size:9.5px;line-height:1.5;color:var(--text,#d8d0c6);border-top:1px solid var(--border,#2a2622);max-height:96px;overflow:auto"></div>';
        stage.appendChild(overlay);
        sg = overlay.querySelector('vera-structgraph');
        var ex = overlay.querySelector('.xp-excerpt');
        overlay.querySelector('.xp-back').onclick = closeOverlay;
        overlay.querySelector('.xp-counts').textContent = '· ' + (doc.cards || []).length + ' cards · ' + (doc.edges || []).length + ' runs · ' + (doc.groups || []).length + ' groups';
        sg.addEventListener('vera-explode-select', function (ev) {
          var d = ev.detail || {}; var h = excerpt(lastDoc, d.span);
          ex.style.display = h ? '' : 'none';
          ex.innerHTML = h ? '<b style="color:var(--acc,#5a9e8f)">' + esc(d.card && d.card.title) + '</b> · ' + esc(d.card && d.card.kind) + ' — ' + h : '';
        });
        sg.addEventListener('vera-explode-verdict', function (ev) {
          var d = ev.detail || {}; var evs = (d.evidence || []).slice(0, 6);
          ex.style.display = '';
          ex.innerHTML = '<b style="color:var(--acc,#5a9e8f)">' + esc(d.label || d.key) + '</b> ' + (d.score != null ? Number(d.score).toFixed(2) : '') +
            ' <span style="color:var(--dim,#6a6058)">· ' + esc(d.by || '') + (d.confidence != null ? ' · confidence ' + d.confidence : '') + '</span>' +
            (evs.length ? '<div style="margin-top:4px">' + evs.map(function (e) { var h = excerpt(lastDoc, e.span); return '<div style="margin:2px 0"><span style="color:var(--dim,#6a6058)">' + esc(e.note || '') + '</span>' + (h ? ' — ' + h : '') + '</div>'; }).join('') + '</div>' : ' — no evidence spans: a composite, or a whole-text measure');
        });
        sg.addEventListener('vera-explode-drill', function (ev) {
          var d = ev.detail || {}; if (!d.card || !d.card.title) return;
          textIn.value = String(d.card.title); whatSel.value = 'text'; syncWhat();
          say('re-explode from a card: paste the passage it stands in — the title alone is too little');
        });
        sg.setDoc(doc);
      }

      // ── Explode ─────────────────────────────────────────────────────────────
      $('.xp-go').onclick = async function () {
        var body = { include_text: true };
        var w = whatSel.value, label = '';
        if (w === 'record') {
          var id = recIn.value.trim() || selectedId();
          if (!id) { say('a record id is needed — or select one on the graph', true); return; }
          body.record_id = id; label = 'record ' + id;
          var rg = parseRanges(rangesIn.value); if (rg.length) { body.ranges = rg; label += ' · ' + rg.map(function (r) { return r[0] + '–' + r[1]; }).join(', '); }
        } else if (w === 'records') {
          var ids = recsIn.value.split(/\n+/).map(function (s) { return s.trim(); }).filter(Boolean);
          if (ids.length < 2) { say('two or more record ids, one per line', true); return; }
          body.record_ids = ids; label = ids.length + ' records';
        } else if (w === 'code') {
          var cpath = $('.xp-code-path').value.trim(), ctext = ($('.xp-code-text').value || '').trim();
          body = {};
          if (ctext) { body.text = ctext; body.lang = $('.xp-code-lang').value; body.path = cpath; label = 'code · ' + (body.lang || 'detected') + ' · ' + ctext.length + ' chars'; }
          else if (cpath) { body.path = cpath; body.depth = $('.xp-code-hop').checked ? 1 : 0; label = cpath; }
          else { say('a repo path or a snippet is needed', true); return; }
        } else {
          var text = (textIn.value || '').trim();
          if (!text) { say('nothing to explode', true); return; }
          body.text = text; label = 'passage · ' + text.length + ' chars';
        }
        if (w !== 'code') {
          if (modeSel.value) body.mode = modeSel.value;
          var L = chosenLayers(); if (L) body.layers = L;
        }
        if ($('.xp-assess').checked) body.assess = true;
        say('exploding…');
        try {
          await ensureStruct();
          var res = await fetch(base + (w === 'code' ? '/code/explode' : '/nlp/explode/prose'), {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
          });
          var doc = await res.json();
          if (!doc || doc.error) { say('explode failed: ' + ((doc && doc.error) || res.status), true); return; }
          lastDoc = doc;
          openOverlay(doc, label);
          var rows = (doc.layers || []).map(function (l) {
            return (l.on ? (l.error ? '✗ ' : '✓ ') : '· ') + esc(l.label || l.id) + (l.on && !l.error ? ' ' + l.count + (l.ms ? ' · ' + l.ms + ' ms' : '') + (l.where ? ' · ' + esc(l.where) : '') : '') + (l.error ? ' — ' + esc(l.error) : '');
          });
          say((doc.counts ? doc.counts.cards + ' cards, ' + doc.counts.edges + ' runs, ' + (doc.counts.paragraphs != null ? doc.counts.paragraphs + ' paragraphs' : doc.counts.files + ' file' + (doc.counts.files === 1 ? '' : 's') + (doc.counts.external ? ', ' + doc.counts.external + ' external' : '')) : '') +
              (doc.source && doc.source.engines ? ' · ' + doc.source.engines.join(' + ') + (doc.source.tree_sitter === false ? ' (tree-sitter not installed)' : '') : '') +
              (doc.source && doc.source.partial ? ' · partial' : '') + ' · not persisted<br>' + rows.join('<br>'));
        } catch (e) {
          say('explode failed: ' + e, true);
        }
      };
    },

    unmount: function (bodyEl) {
      if (bodyEl && bodyEl._xpClose) { try { bodyEl._xpClose(); } catch (e) {} }
      if (bodyEl) bodyEl.innerHTML = '';
    },
  });
})();
