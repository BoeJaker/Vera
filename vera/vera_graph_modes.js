/**
 * vera_graph_modes.js — the display MODES of vera_graph.js (served /ui/vera-graph-modes.js; vera_graph.js loads it)
 * ============================================================================
 * Owner, 2026-09-27: "id like for the exploded view in the chat ui's graphing and the estate 3d and 2d mode to be
 * modules or display modes of vera graph and defined as part of it."
 *
 * Each mode draws the graph's OWN nodes and edges another way, over the stage, through the renderer the product already
 * has for that view - nothing here is a second copy of the maths:
 *   exploded   <vera-exploded> (/ui/exploded_element.js) - the chat's exploded scene: a station per node type, the
 *              type's nodes as what it READ, the types they link to as what it MADE
 *   estate-3d  the one isometric projection (/ui/iso.js, window.VeraISO) - a plate per group (the host a node runs on
 *              when it says, else its type), a block per node, height by how connected it is, colour the graph's
 *   estate-2d  the same blocks seen straight down (the projection at 90°) - the plan
 *   mermaid    <vera-mermaid> (/ui/elements/vera_mermaid.js) - the nodes as a flowchart, a subgraph per type, the
 *              edges with their relation as the label (the diagram technique the chat and the canvas draw with)
 * Hover a block: it lights and says what it is. Click: the graph's detail drawer for that node.
 * A new mode is one registerMode() call (see vera_graph.js, "Display modes").
 */
(function () {
  'use strict';
  if (!window.veraUI || !window.veraUI.Graph || !window.veraUI.Graph.registerMode) return;
  var G = window.veraUI.Graph;

  function need(src, test){
    return new Promise(function(res){
      if (test()) return res(true);
      var s = document.createElement('script'); s.src = src;
      s.onload = function(){ res(!!test()); }; s.onerror = function(){ res(false); };
      (document.head || document.documentElement).appendChild(s);
    });
  }
  function nameOf(n){ return String((n && (n.name || n.label || n.title || n.id)) || 'node'); }
  function typeOf(n){ return String((n && (n.type || (n.labels && n.labels[0]))) || 'node'); }
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){ return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]; }); }

  // ── the estate: 3D and 2D ──────────────────────────────────────────────
  function estate(tilt){
    return function(host, graph, api){
      var stage = document.createElement('div'); stage.style.cssText = 'position:absolute;inset:0';
      var tip = document.createElement('div'); tip.style.cssText = 'position:absolute;z-index:3;pointer-events:none;display:none;max-width:280px;padding:6px 8px;border-radius:6px;background:var(--bg1,#1f1d1a);color:var(--text,#ddd);box-shadow:0 6px 20px rgba(0,0,0,.45),0 0 0 1px var(--border,#333);font:11px/1.4 var(--font-ui,system-ui,sans-serif);white-space:pre-line';
      var note = document.createElement('div'); note.style.cssText = 'position:absolute;left:8px;bottom:6px;z-index:2;font:10px var(--mono,monospace);color:var(--dim2,#999)';
      host.appendChild(stage); host.appendChild(tip); host.appendChild(note);
      var byN = {};
      function draw(){
        need('/ui/iso.js', function(){ return !!window.VeraISO; }).then(function(ok){
          if (!ok) { stage.textContent = 'the iso library (/ui/iso.js) did not load'; return; }
          var ISO = window.VeraISO, N = api.nodes().slice(0, 400), E = api.edges();
          var deg = {}; E.forEach(function(e){ deg[e.from] = (deg[e.from] || 0) + 1; deg[e.to] = (deg[e.to] || 0) + 1; });
          var groups = {}; N.forEach(function(n){ var g = String(n.host || n.node || n.parent || typeOf(n)); (groups[g] = groups[g] || []).push(n); });
          var keys = Object.keys(groups).sort(function(a, b){ return groups[b].length - groups[a].length; });
          var cols = Math.max(1, Math.ceil(Math.sqrt(keys.length))), boxes = [], gu = 0, gv = 0, rowD = 0;
          byN = {};
          keys.forEach(function(g, gi){
            var list = groups[g], side = Math.max(1, Math.ceil(Math.sqrt(list.length))), span = side * 1.25 + 0.5;
            if (gi && gi % cols === 0) { gu = 0; gv += rowD + 1.2; rowD = 0; }
            boxes.push({ u:gu, v:gv, z:0, w:span, d:span, h:0.12, col:'var(--bg2,#2a2622)', t:g, n:'p' + gi });
            list.forEach(function(n, i){
              var id = 'b' + (Object.keys(byN).length + 1); byN[id] = n;
              boxes.push({ u:gu + 0.25 + (i % side) * 1.25, v:gv + 0.25 + Math.floor(i / side) * 1.25, z:0.12, w:0.95, d:0.95,
                           h:0.35 + Math.min(3.2, (deg[n.id] || 0) * 0.3), col:api.color(n), t:nameOf(n), n:id });
            });
            gu += span + 1.2; rowD = Math.max(rowD, span);
          });
          var W = host.clientWidth || 640, H = host.clientHeight || 420, azim = tilt >= 89 ? 0 : 45;
          var k = ISO.isoFitK(boxes, 16, W, H, null, tilt, azim, { max: 60, padb: 24 });
          var faces = ISO.scene(ISO.proj(tilt, azim, k), boxes);
          if (tilt >= 89) faces = faces.filter(function(f){ return f.k === 't'; });   // the plan: the tops
          ISO.fit(faces, W, H, 16); faces = ISO.px(faces);
          stage.innerHTML = faces.map(function(f){
            var node = f.n && f.n.charAt(0) === 'b';
            return '<i data-n="' + esc(f.n) + '" style="position:absolute;left:' + f.x + ';top:' + f.y + ';width:' + f.w + ';height:' + f.h + ';clip-path:' + f.cp + ';background:' + f.col + ';transition:filter .12s' + (node ? ';cursor:pointer' : '') + '"></i>';
          }).join('');
          note.textContent = (tilt >= 89 ? 'estate · 2D plan' : 'estate · 3D') + ' · ' + N.length + ' nodes in ' + keys.length + ' groups' + (api.nodes().length > N.length ? ' (first ' + N.length + ')' : '');
        });
      }
      function lit(id){ Array.prototype.forEach.call(stage.children, function(el){ var on = id && el.getAttribute('data-n') === id; el.style.filter = on ? 'brightness(1.45)' : (id ? 'brightness(.7)' : ''); }); }
      stage.addEventListener('pointermove', function(e){
        var el = e.target && e.target.getAttribute ? e.target : null; var id = el && el.getAttribute('data-n');
        var n = id && byN[id];
        if (!n) { lit(null); tip.style.display = 'none'; return; }
        lit(id);
        var ln = [nameOf(n), typeOf(n)]; ['status', 'state', 'host', 'node', 'ip', 'kind'].forEach(function(k){ if (n[k] != null && n[k] !== '') ln.push(k + ': ' + n[k]); });
        tip.textContent = ln.join('\n') + '\nclick · details'; tip.style.display = 'block';
        var r = host.getBoundingClientRect(); tip.style.left = Math.min(r.width - 290, e.clientX - r.left + 14) + 'px'; tip.style.top = Math.max(4, e.clientY - r.top + 14) + 'px';
      });
      stage.addEventListener('pointerleave', function(){ lit(null); tip.style.display = 'none'; });
      stage.addEventListener('click', function(e){ var id = e.target && e.target.getAttribute && e.target.getAttribute('data-n'); if (id && byN[id]) api.open(byN[id]); });
      var ro = window.ResizeObserver ? new ResizeObserver(function(){ draw(); }) : null; if (ro) ro.observe(host);
      draw();
      return { update: draw, destroy: function(){ if (ro) ro.disconnect(); } };
    };
  }

  // ── exploded: the graph as the chat's exploded scene ─────────────────────
  function exploded(host, graph, api){
    var el = null;
    function scene(){
      var N = api.nodes(), E = api.edges(), byId = {}, types = {};
      N.forEach(function(n){ byId[n.id] = n; var t = typeOf(n); (types[t] = types[t] || []).push(n); });
      var order = Object.keys(types).sort(function(a, b){ return types[b].length - types[a].length; }).slice(0, 12);
      var turns = order.map(function(t, i){
        var links = {}; E.forEach(function(e){ var a = byId[e.from], b = byId[e.to]; if (a && b && typeOf(a) === t && typeOf(b) !== t) links[typeOf(b)] = (links[typeOf(b)] || 0) + 1; });
        return { mid: 't' + i, who: 'graph', t: Date.now(), text: t + ' · ' + types[t].length,
          read: types[t].slice(0, 12).map(function(n){ return { n: nameOf(n).slice(0, 40), d: t, col: api.color(n), kind: 'record' }; }),
          say: [], made: Object.keys(links).sort(function(a, b){ return links[b] - links[a]; }).slice(0, 8).map(function(k){ return { n: k, d: links[k] + ' links', kind: 'note' }; }),
          land: [] };
      });
      return { turns: turns, sel: turns.length ? turns[0].mid : '' };
    }
    function draw(){
      need('/ui/exploded_element.js', function(){ return !!(window.customElements && customElements.get('vera-exploded')); }).then(function(ok){
        if (!ok) { host.textContent = 'the exploded scene (/ui/exploded_element.js) did not load'; return; }
        if (!el) { el = document.createElement('vera-exploded'); el.style.cssText = 'position:absolute;inset:0;display:block'; host.appendChild(el); }
        try { el.setScene(scene()); if (el.fit) el.fit(); } catch(e){}
      });
    }
    draw();
    return { update: draw, destroy: function(){} };
  }

  // ── mermaid: the graph as a flowchart ──────────────────────────────────
  function mermaid(host, graph, api){
    var el = null;
    function code(){
      var N = api.nodes().slice(0, 120), E = api.edges(), ids = {}, byT = {};
      N.forEach(function(n, i){ ids[n.id] = 'n' + i; var t = typeOf(n); (byT[t] = byT[t] || []).push(n); });
      var q = function(s){ return String(s == null ? '' : s).replace(/["\[\]{}()<>|#;]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 40); };
      var lines = ['graph LR'];
      Object.keys(byT).forEach(function(t){ lines.push('subgraph ' + (q(t).replace(/\s+/g, '_') || 'nodes')); byT[t].forEach(function(n){ lines.push('  ' + ids[n.id] + '["' + (q(nameOf(n)) || 'node') + '"]'); }); lines.push('end'); });
      E.slice(0, 300).forEach(function(e){ var a = ids[e.from], b = ids[e.to]; if (!a || !b) return; var r = q(e.rel || ''); lines.push('  ' + a + (r ? ' -->|' + r + '| ' : ' --> ') + b); });
      return lines.join('\n');
    }
    function draw(){
      need('/ui/elements/vera_mermaid.js', function(){ return !!(window.customElements && customElements.get('vera-mermaid')); }).then(function(ok){
        if (!ok) { host.textContent = 'the mermaid renderer (/ui/elements/vera_mermaid.js) did not load'; return; }
        if (!el) { el = document.createElement('vera-mermaid'); el.setAttribute('fill', ''); el.setAttribute('bare', ''); el.style.cssText = 'position:absolute;inset:0;display:block'; host.appendChild(el); }
        try { el.render(code()); } catch(e){}
      });
    }
    draw();
    return { update: draw, destroy: function(){} };
  }

  G.registerMode({ id: 'exploded',  label: 'Exploded',     order: 10, mount: exploded });
  G.registerMode({ id: 'estate-3d', label: 'Estate · 3D', order: 20, mount: estate(30) });
  G.registerMode({ id: 'estate-2d', label: 'Estate · 2D', order: 30, mount: estate(90) });
  G.registerMode({ id: 'mermaid',   label: 'Mermaid',      order: 40, mount: mermaid });
})();
