/* vera/ui/structgraph_element.js — <vera-structgraph>: code and prose as a STRUCTURED diagram (the Explode plan,
   shared-planning/nlp-graphs-code/EXPLODE.md §5). Not a force-directed graph: the design's plates, cards and routed
   runs — the Live Operations board's construction — laid out as BANDS × COLUMNS:

     bands    the groups of the contract (a file and the classes inside it; a paragraph; an entity type), stacked
              top to bottom as plates inside plates, each with its caption inside its top edge;
     columns  the rank — dependency depth for code (importers left, callees right: a dependency reads left → right),
              the entity type or the paragraph for prose — a strip per column across every band;
     cards    the design's dense cards, one per symbol / entity / element: a kind glyph, the title, a mono meta line,
              a few fields, badges, a score bar; every card carries the SPAN it came from, which is what a click hands
              to the host (the code block scrolls, the passage highlights);
     runs     routed by the shared channel router (/ui/routes.js): vertical only in the gutters between columns,
              horizontal only in the channels between bands — "along the lanes of their own plane, then plumb" —
              every leg in a slot of its own, the ports on a card fanned in lane order; a run is styled by its KIND
              (CALLS solid · IMPORTS dashed · INHERITS thick · REFERENCES thin · CO_OCCURS dotted …) and by its
              RESOLUTION: a heuristic edge is dotted, an external one grey — an unmarked guess is worse than no edge.

   The contract (EXPLODE.md §3): { kind, source, layout:{direction, mode}, layers:[{id, label, by, kind, on}],
   groups:[{id, label, kind, parent, span}], cards:[{id, group, layer, kind, title, subtitle, span, fields, badges,
   score, by}], edges:[{from, to, layer, kind, resolution, label, by}], assessments:[{key, label, score, confidence,
   by, on, evidence}] }. Layers are toggled by chips over the stage; a card or edge of a layer that is off is not laid
   out at all. Modes: dependency (code) · position (prose: paragraphs down, entity types across) · type (prose: a band
   per entity type, the paragraphs across). direction TB transposes the whole scene (ports on top and bottom).

   <vera-structgraph [bare] [mode=…] [direction=…]>   .setDoc(contract) · .layers({id: on}) · .mode(name) · .fit()
   events: vera-explode-select {id, span, card} · vera-explode-drill {id, card} · vera-explode-edge {from, to, kind,
   resolution, label} · vera-explode-layers {…} · vera-explode-rendered {cards, edges, size}
   window.VeraStructGraph = { layout, normalise, KIND, version } — layout() is pure (node-testable:
   tests/test_structgraph_layout.cjs). Bare mode (chat, canvas): no toolbar, fitted; full mode (the fabric
   inspector): the toolbar, the layer chips, the legend, the verdict rail, zoom and pan. */
(function (root) {
  'use strict';
  const px = (v) => Math.round(v * 10) / 10;
  const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const routesLib = () => root.VeraRoutes || (typeof require === 'function' && typeof __dirname === 'string' ? require(require('node:path').join(__dirname, 'routes.js')) : null);

  /* ── the vocabulary: an edge kind's colour and class; a card kind's glyph ───────────────────────────────── */
  const KIND = {
    CALLS: ['var(--xp-ac)', 'calls', 'calls'], IMPORTS: ['var(--xp-dv1)', 'imports dash', 'imports'], INHERITS: ['var(--xp-dv2)', 'inherits thick', 'inherits'],
    REFERENCES: ['var(--xp-t3)', 'refs thin', 'references'], CONTAINS: ['var(--xp-t3)', 'contains thin', 'contains'], MATCHES: ['var(--xp-dv3)', 'matches dash', 'matches'],
    RELATES: ['var(--xp-ac2)', 'relates', 'relates to'], MENTIONS: ['var(--xp-t3)', 'mentions thin', 'mentions'], CO_OCCURS: ['var(--xp-t3)', 'cooccurs dot', 'in one sentence'],
    COREF: ['var(--xp-dv1)', 'coref dash', 'the same referent'], SUPPORTS: ['var(--xp-ac2)', 'supports', 'supports'], CONTRADICTS: ['var(--xp-red)', 'contradicts thick', 'contradicts']
  };
  const GLYPH = { function: 'ƒ', method: 'ƒ', class: '◆', module: '▦', file: '▤', variable: '·', external: '⇢', element: '⟨⟩', selector: '#', script: '⚙',
    entity: '●', person: '●', org: '▲', organization: '▲', location: '⌂', date: '◷', event: '✦', claim: '❝', paragraph: '¶', sentence: '·', call: '\u2192', return: '\u21a9', raise: '\u26a0', step: '\u00b7'};
  const glyphOf = (k) => GLYPH[String(k || '').toLowerCase()] || '●';
  const kindCol = (k) => { const s = String(k || '').toLowerCase(); if (/class|inherit/.test(s)) return 'var(--xp-dv2)'; if (/module|file|import/.test(s)) return 'var(--xp-dv1)'; if (/external/.test(s)) return 'var(--xp-t3)';
    if (/person/.test(s)) return 'var(--xp-ac)'; if (/org/.test(s)) return 'var(--xp-dv2)'; if (/location|place/.test(s)) return 'var(--xp-dv3)'; if (/date|time/.test(s)) return 'var(--xp-ac2)'; if (/event/.test(s)) return 'var(--xp-dv1)'; if (/claim/.test(s)) return 'var(--xp-red)'; return 'var(--xp-ac)'; };
  const TYPE_ORDER = ['person', 'org', 'organization', 'location', 'date', 'event', 'claim'];

  /* ── the contract, normalised: ids as strings, every list present, layers derived when absent ────────── */
  function normalise(doc) {
    doc = doc || {}; const S = (v) => String(v == null ? '' : v);
    const groups = (doc.groups || []).map((g, i) => ({ id: S(g.id || ('g' + i)), label: S(g.label || g.id || ''), kind: S(g.kind || 'group'), parent: g.parent == null ? null : S(g.parent), span: g.span || null }));
    const cards = (doc.cards || []).map((c, i) => Object.assign({}, c, { id: S(c.id || ('c' + i)), group: c.group == null ? null : S(c.group), layer: S(c.layer || 'base'), kind: S(c.kind || 'entity'), title: S(c.title || c.id || ''), subtitle: S(c.subtitle || ''), fields: Array.isArray(c.fields) ? c.fields : [], badges: Array.isArray(c.badges) ? c.badges : [] }));
    const edges = (doc.edges || []).map((e) => Object.assign({}, e, { from: S(e.from), to: S(e.to), layer: S(e.layer || 'base'), kind: S(e.kind || 'RELATES').toUpperCase(), resolution: S(e.resolution || 'exact') }));
    // a scorer's receipt (kind 'assessment') is not a layer of cards to toggle: it belongs to the verdict rail
    const scorers = (doc.layers || []).filter((l) => l && l.kind === 'assessment').map((l) => Object.assign({}, l, { id: S(l.id), label: S(l.label || l.id) }));
    let layers = (doc.layers || []).filter((l) => !(l && l.kind === 'assessment')).map((l) => Object.assign({ on: true }, l, { id: S(l.id), label: S(l.label || l.id) }));
    const seen = new Set(layers.map((l) => l.id));
    cards.concat(edges).forEach((x) => { if (!seen.has(x.layer)) { seen.add(x.layer); layers.push({ id: x.layer, label: x.layer, on: true }); } });
    layers.forEach((l) => { l.count = cards.filter((c) => c.layer === l.id).length + edges.filter((e) => e.layer === l.id).length; });
    return { kind: S(doc.kind || (cards.some((c) => /function|class|module/.test(c.kind)) ? 'code' : 'prose')), source: doc.source || {}, layout: Object.assign({ direction: 'LR', mode: '' }, doc.layout || {}), layers, scorers, groups, cards, edges, assessments: doc.assessments || [] };
  }

  /* ── geometry at k = 1 ────────────────────────────────────────────────────────────────────────────────── */
  const K = { CW: 220, CH: 44, FIELD: 13, MAXF: 4, BADGE: 16, VP: 10, HEAD: 26, PAD: 12, INSET: 10, GMIN: 44, LP: 9, CMIN: 30, MX: 26, MY: 26, RAIL: 22,
              CODEW: 300, CODELH: 12, CODEMAX: 22, CODEPAD: 10 };
  /* A CODE CARD SHOWS ITS CODE (owner, 2026-09-22: "id actually like to be able to see the code per card"). The
     extractor puts a symbol's own source on the card (card.code, card.code_line, card.code_more); the card grows to
     it up to CODEMAX lines and says how many it kept back. A card with code is wider — a line of code needs room a
     title does not. */
  const codeLines = (c) => (c && c.code ? Math.min(K.CODEMAX, String(c.code).split('\n').length) : 0);
  const cardW = (c) => (codeLines(c) ? K.CODEW : K.CW);
  const cardH = (c) => K.CH + (c.subtitle ? 0 : -12) + K.FIELD * Math.min(K.MAXF, c.fields.length) + (c.badges.length ? K.BADGE : 0) + (c.score != null ? 6 : 0)
    + (codeLines(c) ? K.CODEPAD + K.CODELH * codeLines(c) + (c.code_more ? K.CODELH : 0) : 0);

  /* A TINY TOKENISER — comment · string · keyword · number · call. No library: a card needs those five, and a
     highlighter pulled from a CDN is not a thing this estate does. It escapes as it goes, so the output is safe. */
  const KW = {
    python: /^(?:def|class|return|if|elif|else|for|while|try|except|finally|with|as|import|from|raise|yield|assert|lambda|pass|break|continue|global|nonlocal|async|await|in|is|not|and|or|None|True|False|self|cls)$/,
    javascript: /^(?:function|return|if|else|for|while|try|catch|finally|switch|case|break|continue|const|let|var|new|class|extends|import|export|from|default|await|async|yield|typeof|instanceof|this|null|undefined|true|false|throw|delete|in|of)$/,
  };
  KW.typescript = KW.javascript; KW.css = /^(?:import|media|supports|keyframes|from|to)$/; KW.html = /^(?:div|span|script|link|body|head|html|section|nav|header|footer|main|form|table)$/;
  const TOK = new RegExp([
    '(/\\*[\\s\\S]*?\\*/|//[^\\n]*|#[^\\n]*)',                                  // comment
    '("""[\\s\\S]*?"""|\'\'\'[\\s\\S]*?\'\'\'|"(?:[^"\\\\\\n]|\\\\.)*"|\'(?:[^\'\\\\\\n]|\\\\.)*\'|`(?:[^`\\\\]|\\\\.)*`)',   // string
    '(\\b\\d[\\w.]*\\b)',                                                        // number
    '([A-Za-z_$][\\w$]*)',                                                      // name
  ].join('|'), 'g');
  function tokenise(code, lang) {
    const kw = KW[String(lang || '').toLowerCase()] || KW.python;
    let out = '', last = 0, m; TOK.lastIndex = 0;
    while ((m = TOK.exec(code))) {
      out += esc(code.slice(last, m.index));
      const t = m[0];
      if (m[1]) out += '<i class="xt-c">' + esc(t) + '</i>';
      else if (m[2]) out += '<i class="xt-s">' + esc(t) + '</i>';
      else if (m[3]) out += '<i class="xt-n">' + esc(t) + '</i>';
      else if (kw.test(t)) out += '<i class="xt-k">' + esc(t) + '</i>';
      else if (code[m.index + t.length] === '(') out += '<i class="xt-f">' + esc(t) + '</i>';
      else out += esc(t);
      last = m.index + t.length;
    }
    return out + esc(code.slice(last));
  }

  /* ── the layout, pure ─────────────────────────────────────────────────────────────────────────────────── */
  function layout(doc, W, H, o) {
    o = o || {}; const D = normalise(doc); const R = routesLib(); if (!R) throw new Error('vera/ui/routes.js is not loaded');
    const off = o.layersOff || {}; const onLayer = {}; D.layers.forEach((l) => { onLayer[l.id] = !off[l.id] && l.on !== false; });
    const cards = D.cards.filter((c) => onLayer[c.layer] !== false); const cid = new Map(cards.map((c) => [c.id, c]));
    /* WHAT IS DRAWN, per layer — never the contract's own count. A chip that reads "imports 2" while both runs are
       hidden (their other end is an external stub that was switched off) tells the reader about something they
       cannot see, and they go looking for it (owner, 2026-09-22). So: count after filtering, and name the layer
       that is hiding the rest. */
    const drawn = {}; const D_ = (id) => (drawn[id] = drawn[id] || { cards: 0, edges: 0, hidden: 0, by: {} });
    D.layers.forEach((l) => D_(l.id));
    D.cards.forEach((c) => { const d = D_(c.layer); if (onLayer[c.layer] !== false) d.cards++; else d.hidden++; });
    // a verdict on a card (complexity, a smell, a clone, tested, provenance) is a badge on it — sized in before layout
    D.assessments.forEach((a) => { const c = a.on && a.on !== 'source' ? cid.get(a.on) : null; if (!c) return; const b = String(a.badge || a.label || a.key); if (c.badges.indexOf(b) < 0) c.badges = c.badges.concat([b]); });
    const edges = D.edges.filter((e) => { const d = D_(e.layer);
      if (onLayer[e.layer] === false) { d.hidden++; return false; }
      if (e.from === e.to) return false;
      // an edge whose other END is hidden is hidden too — and the chip says which layer took it
      const gone = [e.from, e.to].filter((id) => !cid.has(id));
      if (gone.length) { d.hidden++; gone.forEach((id) => { const c = D.cards.find((x) => x.id === id); if (c) d.by[c.layer] = (d.by[c.layer] || 0) + 1; }); return false; }
      d.edges++; return true; });
    const mode = o.mode || D.layout.mode || (D.kind === 'code' ? 'dependency' : 'position'), dir = (o.direction || D.layout.direction || 'LR').toUpperCase();
    const out = { kind: D.kind, mode, direction: dir, plates: [], cards: [], edges: [], ports: [], labels: [], legend: [], layers: D.layers.map((l) => { const d = drawn[l.id] || { cards: 0, edges: 0, hidden: 0, by: {} };
      const byLabel = Object.keys(d.by).map((k) => { const src = D.layers.find((x) => x.id === k); return (src && src.label) || k; });
      return Object.assign({}, l, { on: onLayer[l.id] !== false, drawn: d.cards + d.edges, hidden: d.hidden,
        hiddenBy: byLabel, count: l.count }); }), scorers: D.scorers, assessments: D.assessments.filter((a) => !a.on || a.on === 'source'), size: { w: W, h: H }, runs: 0, back: [] };
    if (!cards.length) return out;
    // ── bands and columns by mode
    const G = new Map(D.groups.map((g) => [g.id, g])); const topOf = (gid) => { let g = G.get(gid), guard = 0; while (g && g.parent != null && G.has(g.parent) && guard++ < 32) g = G.get(g.parent); return g ? g.id : null; };
    const chainOf = (gid) => { const ch = []; let g = G.get(gid), guard = 0; while (g && guard++ < 32) { ch.unshift(g.id); g = g.parent != null ? G.get(g.parent) : null; } return ch; };
    let plates = [];   // {id, label, kind, parent, depth, children:[], cells:{col:[cardId]}}
    const P = new Map(); const plate = (id, label, kind, parent) => { if (P.has(id)) return P.get(id); const p = { id, label, kind, parent, depth: parent ? P.get(parent).depth + 1 : 0, children: [], cells: {}, h: 0 }; P.set(id, p); if (parent) P.get(parent).children.push(p); else plates.push(p); return p; };
    const colOf = new Map(), plateOf = new Map(); let NC = 1, wrapped = 0;
    // the plates a card stands in: its group's chain from the top (parents made before children); none → one bare plate
    const platesOf = (c) => { const ch = c.group != null && G.has(c.group) ? chainOf(c.group) : []; ch.forEach((gid, i) => plate(gid, G.get(gid).label, G.get(gid).kind, i ? ch[i - 1] : null)); return ch.length ? ch[ch.length - 1] : plate('·', '', 'group', null).id; };
    if (mode === 'type') {
      const kinds = []; cards.forEach((c) => { const k = c.kind.toLowerCase(); if (kinds.indexOf(k) < 0) kinds.push(k); }); kinds.sort((a, b) => (TYPE_ORDER.indexOf(a) + 1 || 99) - (TYPE_ORDER.indexOf(b) + 1 || 99));
      kinds.forEach((k) => plate('kind:' + k, k, 'type', null));
      const tops = D.groups.filter((g) => g.parent == null).map((g) => g.id);
      cards.forEach((c) => { plateOf.set(c.id, 'kind:' + c.kind.toLowerCase()); const t = c.group != null ? topOf(c.group) : null; colOf.set(c.id, Math.max(0, tops.indexOf(t))); });
      NC = Math.max(1, tops.length);
    } else if (mode === 'position') {
      const kinds = []; cards.forEach((c) => { const k = c.kind.toLowerCase(); if (kinds.indexOf(k) < 0) kinds.push(k); }); kinds.sort((a, b) => (TYPE_ORDER.indexOf(a) + 1 || 99) - (TYPE_ORDER.indexOf(b) + 1 || 99));
      cards.forEach((c) => { plateOf.set(c.id, platesOf(c)); colOf.set(c.id, kinds.indexOf(c.kind.toLowerCase())); });
      NC = Math.max(1, kinds.length); out.columns = kinds;
    } else if (mode === 'flow') {
      /* FLOW: the inside of ONE function -- its calls in the order the source makes them, each in the branch that
         encloses it, so a reader follows it left to right and sees which calls only happen in the error path.
         Nothing is ranked: the source already decided the order.

         A band counts its OWN steps. Numbering every step on one global axis made a 145-line function a
         50,320px ribbon that fits an ultrawide at 0.07 (measured), because step 120 sat in column 120 however
         short its branch was. Each branch band now starts its own count, so a band is only as wide as the branch
         it draws, and the steps still read left to right inside it; the runs carry the order between bands. */
      const seq = {};
      cards.slice().sort((a, b) => (+a.step || 0) - (+b.step || 0)).forEach((c) => {
        const p = platesOf(c); plateOf.set(c.id, p);
        colOf.set(c.id, (seq[p] = (seq[p] == null ? 0 : seq[p] + 1)));
      });
      NC = Math.max(1, Object.keys(seq).reduce((m, k) => Math.max(m, seq[k]), 0) + 1);
    } else {   // dependency: the groups as plates, the rank as the column
      const directed = edges.filter((e) => /^(CALLS|IMPORTS|INHERITS|REFERENCES|CONTAINS|RELATES|SUPPORTS|CONTRADICTS)$/.test(e.kind));
      const rk = R.rank(cards.map((c) => c.id), directed); out.back = rk.back.map((e) => e.from + '>' + e.to);
      cards.forEach((c) => { plateOf.set(c.id, platesOf(c)); colOf.set(c.id, rk.rank.get(c.id) || 0); });
      NC = Math.max(1, rk.depth);
      /* A RANK IS NOT A COLUMN when the rank is big. A module of 390 symbols is mostly rank 0 or 1, so two columns
         hold nearly every card and the scene is a 28,000px-tall ribbon: on an ultrawide that fits at 0.03 zoom with
         dead air either side, which is the "looks broken on an ultrawide ... it becomes 1 line" the owner reported
         (2026-09-22). Each rank is therefore WRAPPED into as many adjacent columns as it needs to keep its stack
         under one height, chosen so the scene comes out the shape of the STAGE: with a stack of length L (the cards'
         own heights), sub-columns of height T and width w, the scene is (L/T)*w wide by T tall, so T = sqrt(L*w/A)
         for a stage of aspect A. The sub-columns of a rank are adjacent and in order, every later rank shifts right,
         and a rank that already fits is left exactly as it was -- so left-to-right still reads as depth and the
         runs still travel the gutters between columns. */
      const A = Math.max(0.5, o.aspect || (dir === 'TB' ? (H || 900) / Math.max(1, W || 1600) : (W || 1600) / Math.max(1, H || 900)));
      const byRank = new Map(); cards.forEach((c) => { const r = colOf.get(c.id); if (!byRank.has(r)) byRank.set(r, []); byRank.get(r).push(c); });
      const L = cards.reduce((t, c) => t + cardH(c) + K.VP, 0);
      const wide = cards.reduce((t, c) => Math.max(t, cardW(c)), K.CW) + K.GMIN;   // a column is as wide as its widest card
      const nb = Math.max(1, new Set(cards.map((c) => plateOf.get(c.id))).size);   // the bands STACK: each adds its own T
      const T = Math.max(3 * (K.CH + K.VP), Math.sqrt(L * wide / (A * nb)));
      if (o.wrap !== false && L > T * 1.2) {
        wrapped = 1;
        // a rank wraps PER BAND: two bands' cards in one column sit in different bands, so the column is as tall as
        // the taller of them, not the two together
        const ranks = Array.from(byRank.keys()).sort((a2, b2) => a2 - b2);
        let base = 0;
        ranks.forEach((r) => { const acc = {}; const col = {}; let used = 0;
          byRank.get(r).forEach((c) => { const b = plateOf.get(c.id), h = cardH(c) + K.VP;
            if (col[b] == null) { col[b] = 0; acc[b] = 0; }
            if (acc[b] && acc[b] + h > T && col[b] < 40) { col[b]++; acc[b] = 0; }
            acc[b] += h; used = Math.max(used, col[b] + 1); colOf.set(c.id, base + col[b]); });
          base += Math.max(1, used); });
        NC = Math.max(1, base);
      }
    }
    // a plate with nothing in it and no child with anything is not drawn
    const has = (p) => Object.keys(p.cells).length > 0 || p.children.some(has);
    cards.forEach((c) => { const p = P.get(plateOf.get(c.id)); const col = colOf.get(c.id); (p.cells[col] = p.cells[col] || []).push(c.id); });
    plates = plates.filter(has); P.forEach((p) => { p.children = p.children.filter(has); });
    const bandOf = new Map(); plates.forEach((b, bi) => { const walk = (p) => { Object.keys(p.cells).forEach((col) => p.cells[col].forEach((id) => bandOf.set(id, bi))); p.children.forEach(walk); }; walk(b); });
    // ── place: gutters and channels sized to a demand, cells stacked, plates around them
    const pos = new Map();   // card id → {x, y, w, h}
    const place = (need) => {
      const gw = [], ch = []; for (let g = 0; g <= NC; g++) gw.push(Math.max(K.GMIN, ((need && need.gutters[g]) || 0) * K.LP + 16)); for (let c = 0; c <= plates.length; c++) ch.push(Math.max(K.CMIN, ((need && need.channels[c]) || 0) * K.LP + 14));
      // a column is as wide as the widest card in it: a column of code cards is wide, a column of stubs is not
      const colW = []; for (let c = 0; c < NC; c++) colW.push(K.CW);
      cards.forEach((c) => { const col = colOf.get(c.id); colW[col] = Math.max(colW[col] || K.CW, cardW(c)); });
      const xc = []; let x = K.MX; for (let c = 0; c < NC; c++) { x += gw[c]; xc.push(x); x += colW[c]; } const totalW = x + gw[NC] + K.MX;
      const railH = out.assessments.length ? K.RAIL : 0;
      const stackH = (ids) => ids.reduce((s, id, i) => s + cardH(cid.get(id)) + (i ? K.VP : 0), 0);
      const measure = (p) => { const own = Math.max.apply(null, [0].concat(Object.keys(p.cells).map((col) => stackH(p.cells[col])))); p.children.forEach(measure); p.own = own; p.h = K.HEAD + own + (own && p.children.length ? K.VP : 0) + p.children.reduce((s, q) => s + q.h + K.VP, 0) - (p.children.length ? K.VP : 0) + K.PAD; return p.h; };
      // the columns a plate actually occupies, its own and its children's — what its width is made of
      const colsOf = (p) => { const out2 = []; const walk = (q) => { Object.keys(q.cells).forEach((c2) => { if (q.cells[c2].length) out2.push(+c2); }); q.children.forEach(walk); }; walk(p); return out2; };
      const lay = (p, y0) => { const ins = K.MX - 12 + p.depth * K.INSET; const cs = colsOf(p);
        if (cs.length) { const a2 = Math.min.apply(null, cs), b2 = Math.max.apply(null, cs);
          p.x = Math.max(ins, xc[a2] - K.PAD); p.w = Math.min(totalW - p.x - ins, xc[b2] + colW[b2] + K.PAD - p.x); }
        else { p.x = ins; p.w = totalW - 2 * ins; }
        p.y = y0; let y = y0 + K.HEAD;
        Object.keys(p.cells).forEach((col) => { let yy = y; p.cells[col].forEach((id) => { const c = cid.get(id), h = cardH(c); pos.set(id, { x: xc[+col], y: yy, w: cardW(c), h }); yy += h + K.VP; }); });
        y += p.own + (p.own && p.children.length ? K.VP : 0); p.children.forEach((q) => { lay(q, y); y += q.h + K.VP; }); };
      let y = K.MY + railH; const gutters = [], channels = [];
      plates.forEach((b, bi) => { measure(b); channels.push({ y0: y, y1: y + ch[bi] }); y += ch[bi]; lay(b, y); y += b.h; });
      channels.push({ y0: y, y1: y + ch[plates.length] }); const totalH = y + ch[plates.length] + K.MY;
      /* AN ULTRAWIDE STAGE (owner, 2026-09-22: "looks broken on an ultrawide"). One tall stack of bands in a 3440-wide
         window is a narrow strip with 435px of dead air each side, fitted to a zoom the height forced. When the room is
         much wider than the stack wants, the bands FLOW into columns of bands — chosen to be the count that leaves the
         least waste at the zoom it would fit at. Nothing inside a band moves: a band is placed as a whole, so every
         run it holds keeps its routing. */
      for (let g = 0; g <= NC; g++) gutters.push(g < NC ? { x0: xc[g] - gw[g], x1: xc[g] } : { x0: xc[NC - 1] + colW[NC - 1], x1: xc[NC - 1] + colW[NC - 1] + gw[NC] });
      return { gutters, channels, totalW, totalH, xc, gw, ch };
    };
    // ── route: the boxes, the router, the segments
    const route = (geo) => { out.edges = []; out.ports = []; out.runs = 0;
      const boxes = new Map(cards.map((c) => { const p = pos.get(c.id); return [c.id, { id: c.id, cx: p.x + p.w / 2, cy: p.y + p.h / 2, w: p.w, h: p.h, col: colOf.get(c.id), band: bandOf.get(c.id) }]; }));
      const edge = (a, b, col, cls, title) => { const dx = b.x - a.x, dy = b.y - a.y; out.edges.push({ x: px(a.x), y: px(a.y), len: px(Math.sqrt(dx * dx + dy * dy)), deg: +(Math.atan2(dy, dx) * 180 / Math.PI).toFixed(2), col, cls, title }); };
      const CR = R.channelRouter({ gutters: geo.gutters, channels: geo.channels, pitch: K.LP, edge, out });
      edges.forEach((e) => { const st = KIND[e.kind] || KIND.RELATES; const back = out.back.indexOf(e.from + '>' + e.to) >= 0;
        const cls = 'sg-e ' + st[1] + (e.resolution === 'heuristic' ? ' heur' : e.resolution === 'external' ? ' ext' : '') + (back ? ' back' : '') + ' l-' + e.layer.replace(/[^a-z0-9_-]/gi, '_');
        const title = (e.label || st[2]) + (e.resolution !== 'exact' ? ' · ' + e.resolution : '') + (e.by ? ' · ' + e.by : '');
        CR.add(boxes.get(e.from), boxes.get(e.to), e.resolution === 'external' ? 'var(--xp-t3)' : st[0], cls, title, { from: e.from, to: e.to }); });
      CR.flush(); return CR.demand(); };
    // 1. place, 2. order each cell by its neighbours, 3. place again, 4. route for the demand, 5. place to it, 6. route
    let geo = place(null);
    { const cells = []; const walk = (p) => { Object.keys(p.cells).forEach((col) => cells.push({ p, col, ids: p.cells[col] })); p.children.forEach(walk); }; plates.forEach(walk);
      for (let pass = 0; pass < 2; pass++) { const yOf = (id) => { const q = pos.get(id); return q ? q.y + q.h / 2 : NaN; }; const ordered = R.order(cells.map((c) => c.ids), edges, yOf); cells.forEach((c, i) => { c.p.cells[c.col] = ordered[i]; }); geo = place(null); } }
    const need = route(geo); geo = place(need); route(geo);
    /* THE STAGE'S SHAPE. One tall stack in a wide window is a narrow strip with dead air either side, fitted to a
       zoom the HEIGHT forced (owner: "looks broken on an ultrawide"). If the room is much wider than the stack
       wants, the top-level bands flow into columns of bands: each band moves as a WHOLE — its cards, its labels and
       every leg of every run inside it shift with it — so no routing is redone and nothing inside a band changes.
       Runs that cross bands are re-routed afterwards, since their ends have moved. */
    let flowed = 1;
    if (o.flow !== false && plates.length > 1 && W > 0 && H > 0) {
      const fitZoom = (w, h) => Math.min(1, (W - 16) / Math.max(1, w), (H - 40) / Math.max(1, h));
      const bands = plates.map((b) => ({ id: b.id, h: b.h, y: b.y }));
      const totalH0 = geo.totalH, totalW0 = geo.totalW;
      let best = { cols: 1, zoom: fitZoom(totalW0, totalH0), w: totalW0, h: totalH0, rows: null };
      for (let cols = 2; cols <= Math.min(5, plates.length); cols++) {
        // fill a column until adding the next band would take it past the target height — the tallest column is
        // what the fit is decided by, so the aim is the lowest tallest column, not equal columns
        const target = totalH0 / cols;
        const rows = []; let cur = [], acc = 0;
        bands.forEach((b) => { if (cur.length && acc + b.h > target && rows.length < cols - 1) { rows.push(cur); cur = []; acc = 0; }
          cur.push(b); acc += b.h + K.VP; });
        if (cur.length) rows.push(cur);
        if (rows.length < 2) continue;
        const colH = rows.map((r) => r.reduce((t2, b) => t2 + b.h + K.VP, 0));
        const w = totalW0 * rows.length + K.MX * (rows.length - 1), h = Math.max.apply(null, colH) + K.MY * 2;
        const z = fitZoom(w, h);
        if (z > best.zoom * 1.02) best = { cols: rows.length, zoom: z, w, h, rows };
      }
      if (best.rows) {
        /* each band moves as a WHOLE: its plate subtree and every card position inside it get one offset, so
           nothing inside a band is re-laid-out. The runs are routed again afterwards over the moved ends. */
        const top = K.MY + (out.assessments.length ? K.RAIL : 0);
        const dxOf = {}, dyOf = {}, colGut = [], colChan = [{ y0: 0, y1: top }];
        best.rows.forEach((col, ci) => { const dx = ci * (totalW0 + K.MX); let yy = top;
          geo.gutters.forEach((g) => colGut.push({ x0: g.x0 + dx, x1: g.x1 + dx }));
          col.forEach((b) => { dxOf[b.id] = dx; dyOf[b.id] = yy - b.y; yy += b.h + K.VP; colChan.push({ y0: yy - K.VP, y1: yy }); }); });
        plates.forEach((b) => { const dx = dxOf[b.id] || 0, dy = dyOf[b.id] || 0;
          const walk = (p) => { p.x += dx; p.y += dy;
            Object.keys(p.cells).forEach((c2) => p.cells[c2].forEach((id) => { const q = pos.get(id); if (q) { q.x += dx; q.y += dy; } }));
            p.children.forEach(walk); };
          walk(b); });
        const seen = {}, chan = colChan.filter((c2) => { const k2 = c2.y0 + ':' + c2.y1; if (seen[k2]) return false; seen[k2] = 1; return true; });
        geo = { gutters: colGut.sort((a2, b2) => a2.x0 - b2.x0), channels: chan.sort((a2, b2) => a2.y0 - b2.y0),
                totalW: best.w, totalH: best.h, xc: geo.xc, gw: geo.gw, ch: geo.ch };
        route(geo);
        flowed = best.rows.length;
      }
    }
    // ── the scene
    const walkPlates = (p) => { out.plates.push({ id: p.id, label: p.label, kind: p.kind, depth: p.depth, x: px(p.x), y: px(p.y), w: px(p.w), h: px(p.h), cls: 'sg-pl d' + p.depth + ' k-' + p.kind.replace(/[^a-z0-9_-]/gi, '_') });
      out.labels.push({ plate: p.id, x: px(p.x + 12), y: px(p.y + 8), n: p.label || p.id, k: p.kind, depth: p.depth }); p.children.forEach(walkPlates); };
    plates.forEach(walkPlates);
    cards.forEach((c) => { const p = pos.get(c.id); out.cards.push({ id: c.id, x: px(p.x), y: px(p.y), w: p.w, h: px(p.h), col: colOf.get(c.id), band: bandOf.get(c.id), plate: plateOf.get(c.id), card: c, glyph: glyphOf(c.kind), colr: kindCol(c.kind), cls: 'sg-card k-' + c.kind.toLowerCase().replace(/[^a-z0-9_-]/g, '_') + ' l-' + c.layer.replace(/[^a-z0-9_-]/gi, '_') + (c.kind.toLowerCase() === 'external' ? ' ext' : '') }); });
    out.verdicts = {};   // per-card assessments, by card id — the element shows them on hover
    D.assessments.forEach((a) => { if (a.on && a.on !== 'source' && cid.has(a.on)) (out.verdicts[a.on] = out.verdicts[a.on] || []).push(a); });
    if (mode === 'position' && out.columns && flowed === 1) out.columns.forEach((k, i) => out.labels.push({ column: i, x: px(geo.xc[i]), y: px(K.MY + (out.assessments.length ? K.RAIL : 0) + 6), n: k, k: 'column' }));
    const kinds = {}; edges.forEach((e) => { kinds[e.kind] = (kinds[e.kind] || 0) + 1; }); out.legend = Object.keys(kinds).map((k) => ({ kind: k, n: kinds[k], col: (KIND[k] || KIND.RELATES)[0], cls: (KIND[k] || KIND.RELATES)[1], label: (KIND[k] || KIND.RELATES)[2] }));
    out.size = { w: px(geo.totalW), h: px(geo.totalH) }; out.geom = { columns: NC, bands: plates.length, bandColumns: flowed, gutters: geo.gw, channels: geo.ch, xc: geo.xc };
    /* ONE CORRECTION PASS. The wrap aims the scene at the shape of the stage, but a band's height is not known
       until the runs are routed: the channels between the bands widen to the demand of what crosses them, and on a
       busy graph that is hundreds of pixels the aim never saw. So if the scene came out well off the shape it was
       aimed at, aim once more with the miss divided out. Once only, and only when the wrap is what decided the
       shape -- a scene that is one column of cards is the shape it is. */
    if (wrapped && !o._aimed) {
      const want = dir === 'TB' ? (H || 900) / Math.max(1, W || 1600) : (W || 1600) / Math.max(1, H || 900);
      const got = geo.totalW / Math.max(1, geo.totalH);
      if (got / want < 0.85 || got / want > 1.25) return layout(doc, W, H, Object.assign({}, o, { _aimed: 1, aspect: want * want / got }));
    }
    if (dir === 'TB') transpose(out);
    return out;
  }
  // direction TB: the same scene with the axes swapped — bands side by side, columns down, ports on top and bottom
  function transpose(out) {
    const sw = (o) => { const x = o.x; o.x = o.y; o.y = x; if (o.w != null) { const w = o.w; o.w = o.h; o.h = w; } };
    out.plates.forEach(sw); out.cards.forEach(sw); out.labels.forEach(sw); out.ports.forEach((p) => { sw(p); p.side = p.side === 'R' ? 'B' : p.side === 'L' ? 'T' : p.side; });
    out.edges.forEach((e) => { sw(e); e.deg = +(90 - e.deg).toFixed(2); if (e.deg > 180) e.deg -= 360; });
    sw(out.size);
  }

  /* ── the element ──────────────────────────────────────────────────────────────────────────────────────── */
  const CSS = `
vera-structgraph{display:flex;flex-direction:column;min-height:0;min-width:0;position:relative;overflow:hidden;--xp-bg:var(--bg0,#0e0f12);--xp-s1:var(--bg1,#15171c);--xp-s2:var(--bg2,#1b1e25);--xp-s3:var(--bg3,#232732);--xp-bd:var(--border,#2a2e37);--xp-bd2:var(--border2,#3a3f4b);--xp-t1:var(--fg,#e6e6e6);--xp-t2:var(--dim,#aaa);--xp-t3:var(--dim2,#777);--xp-ac:var(--acc,#7c9cff);--xp-ac2:var(--acc2,#5ec9a0);--xp-dv1:#a78bfa;--xp-dv2:#fb923c;--xp-dv3:#38bdf8;--xp-red:#e06c75;--xp-mono:var(--mono,ui-monospace,monospace);font-size:10.5px;color:var(--xp-t1);background:var(--xp-bg)}
vera-structgraph .sg-ctl{position:absolute;left:12px;top:10px;z-index:30;display:flex;align-items:center;flex-wrap:wrap;gap:5px;padding:5px 8px;border-radius:8px;background:color-mix(in srgb,var(--xp-s1) 92%,transparent);box-shadow:0 0 0 1px var(--xp-bd);max-width:calc(100% - 24px)}
vera-structgraph .sg-ctl .c{font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--xp-t3);margin-right:3px}
vera-structgraph .sg-ctl button{font:inherit;font-size:10px;color:var(--xp-t2);background:none;border:0;cursor:pointer;padding:3px 10px;border-radius:999px}
vera-structgraph .sg-ctl button.on{background:var(--xp-ac);color:var(--xp-bg)}vera-structgraph .sg-ctl .sep{width:1px;height:14px;background:var(--xp-bd);margin:0 3px}
vera-structgraph .sg-chip{display:inline-flex;align-items:center;gap:5px;font:inherit;font-size:10px;color:var(--xp-t3);background:none;border:0;cursor:pointer;padding:3px 8px;border-radius:999px}vera-structgraph .sg-chip i{width:6px;height:6px;border-radius:2px;background:var(--lc,var(--xp-ac));opacity:.35}vera-structgraph .sg-chip b{font-family:var(--xp-mono);font-size:9px;font-weight:400;color:var(--xp-t3)}
vera-structgraph .sg-chip.on{color:var(--xp-t1);background:var(--xp-s2)}vera-structgraph .sg-chip.off b{color:var(--xp-t3)}vera-structgraph .sg-chip .hid{font-style:normal;color:var(--xp-dv2);margin-left:3px;font-size:10px}vera-structgraph .sg-chip.on i{opacity:1}vera-structgraph .sg-chip:hover{color:var(--xp-t1)}
vera-structgraph .sg-key{position:absolute;right:12px;bottom:10px;z-index:30;display:flex;flex-direction:column;gap:3px;padding:6px 9px;border-radius:8px;background:color-mix(in srgb,var(--xp-s1) 92%,transparent);box-shadow:0 0 0 1px var(--xp-bd);font-size:9.5px;color:var(--xp-t2)}
vera-structgraph .sg-key .c{font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--xp-t3)}vera-structgraph .sg-key span{display:flex;align-items:center;gap:7px}vera-structgraph .sg-key span i{display:inline-block;width:22px;height:0;border-top:1.5px solid var(--kc)}vera-structgraph .sg-key span.dash i{border-top-style:dashed}vera-structgraph .sg-key span.dot i{border-top-style:dotted}vera-structgraph .sg-key span.thick i{border-top-width:3px}vera-structgraph .sg-key span b{font-family:var(--xp-mono);font-weight:400;color:var(--xp-t3)}
vera-structgraph .sg-wrap{flex:1;min-height:0;position:relative;overflow:hidden;cursor:grab;touch-action:none;user-select:none}vera-structgraph .sg-wrap.dragging{cursor:grabbing}
vera-structgraph .sg-view{position:absolute;left:0;top:0;transform-origin:0 0}
vera-structgraph .sg-space{position:absolute;left:0;top:0;width:0;height:0;pointer-events:none}
vera-structgraph .sg-wrap.scroll{overflow:auto;scrollbar-width:thin}   /* a keyboard/trackpad fallback; the drag still pans */
vera-structgraph .sg-pl{position:absolute;pointer-events:none;border-radius:8px;--pc:var(--xp-ac);background:color-mix(in srgb,var(--pc) 5%,var(--xp-s1));box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--pc) 22%,transparent)}
vera-structgraph .sg-pl.d1{--pc:var(--xp-dv1);background:color-mix(in srgb,var(--pc) 5%,transparent)}vera-structgraph .sg-pl.d2{--pc:var(--xp-dv3);background:color-mix(in srgb,var(--pc) 4%,transparent)}
vera-structgraph .sg-pl.k-type,vera-structgraph .sg-pl.k-paragraph{--pc:var(--xp-ac2)}
vera-structgraph .sg-lb{position:absolute;pointer-events:none;font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:color-mix(in srgb,var(--pc,var(--xp-ac)) 80%,var(--xp-t2));white-space:nowrap}vera-structgraph .sg-lb b{font-family:var(--xp-mono);font-weight:400;letter-spacing:0;text-transform:none;color:var(--xp-t3);margin-left:8px}
vera-structgraph .sg-lb.column{color:var(--xp-t3);letter-spacing:.12em}
vera-structgraph .sg-card{position:absolute;box-sizing:border-box;padding:5px 9px 5px 11px;border-radius:6px;background:var(--xp-s2);box-shadow:0 0 0 1px var(--xp-bd),inset 3px 0 0 0 var(--cc);overflow:hidden;cursor:pointer;transition:box-shadow .15s}
vera-structgraph .sg-card:hover,vera-structgraph .sg-card.lit{box-shadow:0 0 0 1.5px var(--cc),inset 3px 0 0 0 var(--cc)}vera-structgraph .sg-card.sel{box-shadow:0 0 0 2px var(--xp-ac),inset 3px 0 0 0 var(--cc)}
vera-structgraph .sg-card.ext{opacity:.62;background:var(--xp-s1)}vera-structgraph .sg-card.dim{opacity:.28}
vera-structgraph .sg-card .n{display:flex;align-items:baseline;gap:6px;font-size:11.5px;font-weight:600;line-height:1.25;color:var(--xp-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}vera-structgraph .sg-card .n .g{color:var(--cc);font-weight:400;font-size:11px;flex:none}
vera-structgraph .sg-card .m{font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}
vera-structgraph .sg-card .f{display:grid;grid-template-columns:auto 1fr;gap:0 8px;margin-top:3px;font-family:var(--xp-mono);font-size:9px;line-height:13px;color:var(--xp-t2)}vera-structgraph .sg-card .f b{font-weight:400;color:var(--xp-t3)}vera-structgraph .sg-card .f span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
vera-structgraph .sg-card .b{display:flex;gap:4px;margin-top:3px;flex-wrap:nowrap;overflow:hidden}vera-structgraph .sg-card .b i{font-style:normal;font-size:8.5px;font-family:var(--xp-mono);padding:1px 5px;border-radius:999px;background:var(--xp-s3);color:var(--xp-t2);white-space:nowrap}vera-structgraph .sg-card .b i.warn{background:color-mix(in srgb,var(--xp-dv2) 25%,var(--xp-s3));color:var(--xp-dv2)}vera-structgraph .sg-card .b i.ok{background:color-mix(in srgb,var(--xp-ac2) 22%,var(--xp-s3));color:var(--xp-ac2)}
vera-structgraph .sg-card .code{margin-top:5px;padding-top:4px;border-top:1px solid var(--xp-bd);font-family:var(--xp-mono);font-size:9.5px;line-height:12px;white-space:pre;overflow:hidden}
vera-structgraph .sg-card .code .cl{display:block;color:var(--xp-t2);float:none;width:auto;min-width:0;padding:0;margin:0}
vera-structgraph .sg-card .code .ln{display:inline-block;width:2.2em;margin-right:.6em;text-align:right;font-style:normal;color:var(--xp-t3);opacity:.45;user-select:none}
vera-structgraph .sg-card .code .cl.more{color:var(--xp-t3);font-style:italic;opacity:.7;padding-left:2.8em}
/* A TOKEN IS A TOKEN, whatever page hosts the diagram. The tokeniser's classes were single letters, and in the
   chat '.f' is display:grid (measured live, 2026-09-22) — so every function name became a full-width block and
   one line of source drew as three, 40px tall where 12 was laid out for it. The classes are namespaced, and the
   tokens are reset besides: nothing a host page says about an <i> should reach inside the code on a card. */
vera-structgraph .sg-card .code i{font-style:normal;display:inline;float:none;margin:0;padding:0;border:0;min-width:0;width:auto;height:auto;background:none;position:static;flex:none}
vera-structgraph .sg-card .code .xt-c{color:var(--xp-t3);opacity:.8}
vera-structgraph .sg-card .code .xt-s{color:var(--xp-ac2)}
vera-structgraph .sg-card .code .xt-k{color:var(--xp-dv1)}
vera-structgraph .sg-card .code .xt-n{color:var(--xp-dv2)}
vera-structgraph .sg-card .code .xt-f{color:var(--xp-ac)}
/* a lint finding marks its own line, and says what it is on hover */
vera-structgraph .sg-card .code .cl.mk{background:color-mix(in srgb,var(--xp-dv2) 14%,transparent);border-left:2px solid var(--xp-dv2);margin-left:-2px}
vera-structgraph .sg-card .code .cl.mk.error{background:color-mix(in srgb,var(--xp-red) 16%,transparent);border-left-color:var(--xp-red)}
vera-structgraph .sg-card .code .cl.mk .ln{opacity:1;color:var(--xp-dv2)}
vera-structgraph .sg-card .bar{height:3px;border-radius:2px;background:var(--xp-s3);margin-top:4px;overflow:hidden}vera-structgraph .sg-card .bar i{display:block;height:100%;background:var(--cc)}
vera-structgraph .sg-e{position:absolute;height:0;border-top:1.5px solid var(--ec);transform-origin:0 0;pointer-events:auto;z-index:2;opacity:.85}
vera-structgraph .sg-e::after{content:"";position:absolute;left:0;right:0;top:-5px;height:10px}
vera-structgraph .sg-e.dash{border-top-style:dashed}vera-structgraph .sg-e.dot,vera-structgraph .sg-e.heur{border-top-style:dotted}vera-structgraph .sg-e.thin{border-top-width:1px;opacity:.6}vera-structgraph .sg-e.thick{border-top-width:2.5px}vera-structgraph .sg-e.ext{opacity:.4}vera-structgraph .sg-e.back{opacity:.55}
vera-structgraph .sg-e.lit{opacity:1;border-top-width:2.5px;z-index:3;filter:drop-shadow(0 0 2px var(--ec))}vera-structgraph .sg-e.dim{opacity:.12}
vera-structgraph .sg-port{position:absolute;width:5px;height:5px;border-radius:50%;background:var(--xp-bg);box-shadow:0 0 0 1.5px var(--xp-t3);transform:translate(-50%,-50%);z-index:4;pointer-events:none}
vera-structgraph .sg-vr{position:absolute;display:flex;gap:6px;align-items:center;z-index:5}vera-structgraph .sg-vr .c{font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--xp-t3);margin-right:2px}
vera-structgraph .sg-vr span{display:inline-flex;align-items:center;gap:5px;font-size:9.5px;color:var(--xp-t2);padding:2px 8px;border-radius:999px;background:var(--xp-s2);box-shadow:0 0 0 1px var(--xp-bd);cursor:default;pointer-events:auto}vera-structgraph .sg-vr span.has-ev{cursor:pointer}vera-structgraph .sg-vr span.has-ev:hover{color:var(--xp-t1);box-shadow:0 0 0 1px var(--xp-ac)}vera-structgraph .sg-vr span i{display:inline-block;width:34px;height:3px;border-radius:2px;background:var(--xp-s3);overflow:hidden}vera-structgraph .sg-vr span i b{display:block;height:100%;background:var(--xp-ac2)}vera-structgraph .sg-vr span em{font-style:normal;font-family:var(--xp-mono);color:var(--xp-t3)}
vera-structgraph .sg-pz{position:absolute;right:12px;top:10px;z-index:30;display:flex;align-items:center;gap:2px;padding:3px 6px;border-radius:8px;background:color-mix(in srgb,var(--xp-s1) 92%,transparent);box-shadow:0 0 0 1px var(--xp-bd)}vera-structgraph .sg-pz button{font:inherit;font-size:11px;color:var(--xp-t2);background:none;border:0;cursor:pointer;padding:1px 7px;border-radius:6px}vera-structgraph .sg-pz .z{font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3);min-width:34px;text-align:center}
vera-structgraph .sg-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--xp-t3);font-size:11px}
vera-structgraph[bare] .sg-ctl,vera-structgraph[bare] .sg-pz,vera-structgraph[bare] .sg-key{display:none}`;
  function ensureCss(doc) { doc = doc || document; if (doc.getElementById('vera-structgraph-css')) return; const s = doc.createElement('style'); s.id = 'vera-structgraph-css'; s.textContent = CSS; (doc.head || doc.documentElement).appendChild(s); }
  function ensureRoutes(doc, onload) { doc = doc || document; if (root.VeraRoutes || doc.getElementById('vera-routes-lib')) return; const s = doc.createElement('script'); s.id = 'vera-routes-lib'; s.src = '/ui/routes.js'; s.async = true; s.onload = () => { try { onload && onload(); } catch (_) {} }; (doc.head || doc.documentElement).appendChild(s); }

  function cardHtml(k) { const c = k.card; let h = '<div class="n"><span class="g">' + esc(k.glyph) + '</span><span>' + esc(c.title) + '</span></div>';
    if (c.subtitle) h += '<div class="m">' + esc(c.subtitle) + (c.by ? ' · ' + esc(c.by) : '') + '</div>';
    if (c.fields.length) h += '<div class="f">' + c.fields.slice(0, K.MAXF).map((f) => '<b>' + esc(f.k) + '</b><span>' + esc(f.v) + '</span>').join('') + '</div>';
    if (c.badges.length) h += '<div class="b">' + c.badges.slice(0, 5).map((b) => '<i class="' + (/^(partial|error|warn|smell|clone|cc )/i.test(String(b)) ? 'warn' : /^(tested|retyped)/i.test(String(b)) ? 'ok' : '') + '">' + esc(b) + '</i>').join('') + '</div>';
    if (c.score != null) h += '<div class="bar"><i style="width:' + Math.round(Math.max(0, Math.min(1, +c.score)) * 100) + '%"></i></div>';
    if (c.code) { const lines = String(c.code).split('\n').slice(0, K.CODEMAX); const l0 = c.code_line || 1;
      const marks = c.marks || k.marks || {};         // line number -> [{note, sev}], from the lint layer
                                                      // (on the CARD of the contract: the laid-out card only wraps it)
      h += '<div class="code" data-lang="' + esc(c.lang || '') + '">' + lines.map((l, i) => { const n = l0 + i; const mk = marks[n];
        return '<span class="cl' + (mk ? ' mk ' + esc(mk[0].sev || 'warn') : '') + '" data-line="' + n + '"'
          + (mk ? ' title="' + esc(mk.map((x) => x.note).join(' · ')) + '"' : '') + '><i class="ln">' + n + '</i>' + tokenise(l, c.lang) + '</span>';
      }).join('') + (c.code_more ? '<span class="cl more">' + c.code_more + ' more lines</span>' : '') + '</div>'; }
    return h; }
  function sceneHtml(o) { let h = '';
    o.plates.forEach((p) => { h += '<div class="' + p.cls + '" style="left:' + p.x + 'px;top:' + p.y + 'px;width:' + p.w + 'px;height:' + p.h + 'px"></div>'; });
    o.labels.forEach((l) => { h += '<div class="sg-lb' + (l.column != null ? ' column' : '') + '" style="left:' + l.x + 'px;top:' + l.y + 'px">' + esc(l.n) + (l.k && l.column == null ? '<b>' + esc(l.k) + '</b>' : '') + '</div>'; });
    o.edges.forEach((e) => { h += '<div class="' + e.cls + '" data-from="' + esc(e.from) + '" data-to="' + esc(e.to) + '" data-run="' + e.run + '" title="' + esc(e.title) + '" style="--ec:' + e.col + ';left:' + e.x + 'px;top:' + e.y + 'px;width:' + e.len + 'px;transform:rotate(' + e.deg + 'deg)"></div>'; });
    o.ports.forEach((p) => { h += '<div class="sg-port" data-run="' + p.run + '" style="left:' + p.x + 'px;top:' + p.y + 'px"></div>'; });
    o.cards.forEach((k) => { h += '<div class="' + k.cls + '" data-id="' + esc(k.id) + '" style="--cc:' + k.colr + ';left:' + k.x + 'px;top:' + k.y + 'px;width:' + k.w + 'px;height:' + k.h + 'px" title="' + esc(k.card.title + (k.card.span ? ' · ' + (k.card.span.path || '') + ' ' + (k.card.span.start != null ? k.card.span.start + '–' + k.card.span.end : '') : '')) + '">' + cardHtml(k) + '</div>'; });
    if (o.assessments.length) h += '<div class="sg-vr" style="left:' + K.MX + 'px;top:' + (K.MY - 6) + 'px"><span class="c">verdict</span>' + o.assessments.map((a, i) => '<span data-k="' + i + '" class="' + ((a.evidence || []).length ? 'has-ev' : '') + '" title="' + esc((a.by || '') + (a.confidence != null ? ' · confidence ' + a.confidence : '') + ((a.evidence || []).length ? ' · click: the evidence' : '')) + '">' + esc(a.label || a.key) + '<i><b style="width:' + Math.round(Math.max(0, Math.min(1, +a.score || 0)) * 100) + '%"></b></i><em>' + (a.score != null ? (+a.score).toFixed(2) : '') + '</em></span>').join('') + '</div>';
    return h; }

  if (typeof HTMLElement !== 'undefined' && root.customElements && !root.customElements.get('vera-structgraph')) {
    class VeraStructGraph extends HTMLElement {
      constructor() { super(); this._S = { doc: null, layersOff: {}, mode: '', direction: '', zoom: 1, px: 0, py: 0, fit: true, sel: null }; }
      static get observedAttributes() { return ['mode', 'direction']; }
      attributeChangedCallback(n, _o, v) { if (n === 'mode') this._S.mode = v || ''; if (n === 'direction') this._S.direction = v || ''; this._schedule(); }
      setDoc(doc) { this._S.doc = doc; this._S.fit = true; this._schedule(); return this; }
      set data(doc) { this.setDoc(doc); } get data() { return this._S.doc; }
      layers(m) { if (m) Object.keys(m).forEach((k) => { this._S.layersOff[k] = !m[k]; }); this._schedule(); this.dispatchEvent(new CustomEvent('vera-explode-layers', { detail: Object.assign({}, this._S.layersOff), bubbles: true })); return this._S.layersOff; }
      mode(name) { if (name) { this._S.mode = name; this._S.fit = true; this._schedule(); } return this._S.mode; }
      fit() { this._S.fit = true; this._schedule(); }
      /* ── the host's side of the span binding ─────────────────────────────
         lightSpan({path, start, end} | {line, line_end}) — light every card whose span covers that part of the
         source and quiet the rest, the way clicking a card lights its runs. The host (a canvas block beside a
         code block, a chat turn beside its snippet) calls this when the READER moves in the source; the element
         answers the other way with vera-explode-select. Character offsets when it has them, line numbers when it
         does not (a code card carries both). Returns the ids it lit; null or no hit clears. */
      lightSpan(span) {
        const o = this._last;
        if (!o) return [];
        if (!span) { this._extHit = null; this._paint(); return []; }
        const path = span.path == null ? null : String(span.path);
        const hasCh = span.start != null && span.end != null;
        const s = +span.start, e = +span.end, l0 = +(span.line || 0), l1 = +(span.line_end || span.line || 0);
        const hit = o.cards.filter((k) => { const sp = (k.card && k.card.span) || {};
          if (path != null && String(sp.path || '') !== path) return false;
          if (hasCh && sp.start != null && sp.end != null) return sp.start < e && s < sp.end;
          if (l0 && sp.line != null) return sp.line <= l1 && l0 <= (sp.line_end || sp.line);
          return false; }).map((k) => k.id);
        // the innermost card wins the selection: a method inside a class inside a file is what the reader meant
        const inner = hit.slice().sort((a, b) => { const A = this._cardOf(a).card.span, B = this._cardOf(b).card.span;
          return ((A.end - A.start) || 0) - ((B.end - B.start) || 0); })[0] || null;
        this._extHit = { ids: new Set(hit), sel: inner };
        this._paint();
        return hit;
      }
      state() { return Object.assign({}, this._S, { last: this._last }); }
      connectedCallback() {
        ensureCss(this.ownerDocument); ensureRoutes(this.ownerDocument, () => this._schedule()); if (this._built) { this._schedule(); return; } this._built = true;
        this._S.mode = this.getAttribute('mode') || ''; this._S.direction = this.getAttribute('direction') || '';
        this.innerHTML = '<div class="sg-ctl" data-r="ctl"></div><div class="sg-pz"><button data-a="zout" title="Zoom out">−</button><span class="z" data-r="zoom">100%</span><button data-a="zin" title="Zoom in">+</button><button data-a="fit" title="Fit the whole diagram">fit</button></div><div class="sg-wrap" data-r="wrap"><div class="sg-space" data-r="space"></div><div class="sg-view" data-r="view"></div></div><div class="sg-key" data-r="key"></div>';
        const $ = (r) => this.querySelector('[data-r="' + r + '"]'); const wrap = $('wrap');
        this.addEventListener('click', (ev) => { const b = ev.target.closest('button'); if (b && b.dataset.a) { const a = b.dataset.a; if (a === 'zin') this._zoomBy(1.2); else if (a === 'zout') this._zoomBy(1 / 1.2); else if (a === 'fit') this.fit(); return; }
          if (b && b.dataset.m) { this.mode(b.dataset.m); return; } if (b && b.dataset.l) { const id = b.dataset.l, m = {}; m[id] = !!this._S.layersOff[id]; this.layers(m); return; }
          const vr = ev.target.closest('.sg-vr span[data-k]'); if (vr && this._last) { const a = this._last.assessments[+vr.dataset.k]; if (!a) return;
            // the verdict's evidence: the cards whose spans it falls in are lit, the rest quiet; the host hears the spans
            const evs = (a.evidence || []).filter((e) => e && e.span); const hit = new Set();
            this._last.cards.forEach((k) => { const s = k.card.span || {}; if (evs.some((e) => (e.span.path || '') === (s.path || '') && e.span.start < s.end && s.start < e.span.end)) hit.add(k.id); });
            this._S.sel = null; this._hover = null; this._paint(); this.querySelectorAll('.sg-card').forEach((c) => { c.classList.toggle('lit', hit.has(c.dataset.id)); c.classList.toggle('dim', evs.length > 0 && !hit.has(c.dataset.id)); });
            this.dispatchEvent(new CustomEvent('vera-explode-verdict', { detail: { key: a.key, label: a.label, score: a.score, confidence: a.confidence, by: a.by, evidence: evs }, bubbles: true })); return; }
          if (this._dragEnded && Date.now() - this._dragEnded < 250) return;   // the pointer came up from a pan
          const card = ev.target.closest('.sg-card'); if (card) { const k = this._cardOf(card.dataset.id); this._S.sel = card.dataset.id; this._extHit = null; this._paint(); this.dispatchEvent(new CustomEvent('vera-explode-select', { detail: { id: card.dataset.id, span: k && k.card.span, card: k && k.card }, bubbles: true })); return; }
          const e = ev.target.closest('.sg-e'); if (e) { const seg = this._last && this._last.edges.find((s) => String(s.run) === e.dataset.run); this.dispatchEvent(new CustomEvent('vera-explode-edge', { detail: { from: e.dataset.from, to: e.dataset.to, title: seg && seg.title, run: +e.dataset.run }, bubbles: true })); } });
        this.addEventListener('dblclick', (ev) => { const card = ev.target.closest('.sg-card'); if (!card) return; const k = this._cardOf(card.dataset.id); this.dispatchEvent(new CustomEvent('vera-explode-drill', { detail: { id: card.dataset.id, card: k && k.card }, bubbles: true })); });
        this.addEventListener('mouseover', (ev) => { const card = ev.target.closest('.sg-card'); this._hover = card ? card.dataset.id : null; this._paint(); });
        this.addEventListener('mouseleave', () => { this._hover = null; this._paint(); });
        /* THE STAGE: drag pans, always. A big scene used to put the wrap into a scroll container and the drag
           returned early — so panning died exactly when the diagram was large enough to need it (owner, 2026-09-22).
           A diagram is a surface you move, not a page you scroll: the drag always pans, the wheel always zooms about
           the pointer, shift+wheel slides sideways, and the scrollbars stay only as a keyboard/trackpad fallback —
           any drag cancels the scroll offset so the two never fight. */
        let drag = null;
        wrap.addEventListener('pointerdown', (ev) => { if (ev.button !== 0 || ev.target.closest('.sg-card,.sg-vr,button')) return;
          drag = { x: ev.clientX, y: ev.clientY, px: this._S.px, py: this._S.py, moved: false };
          wrap.classList.add('dragging'); try { wrap.setPointerCapture(ev.pointerId); } catch (_) {} });
        wrap.addEventListener('pointermove', (ev) => { if (!drag) return;
          const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
          if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 3) return;          // a click is not a pan
          if (!drag.moved) { drag.moved = true; if (wrap.scrollLeft || wrap.scrollTop) { drag.px -= wrap.scrollLeft; drag.py -= wrap.scrollTop; wrap.scrollLeft = 0; wrap.scrollTop = 0; } }
          this._S.px = drag.px + dx; this._S.py = drag.py + dy; this._S.fit = false; this._place(); });
        const endDrag = () => { if (!drag) return; const moved = drag.moved; drag = null; wrap.classList.remove('dragging'); if (moved) this._dragEnded = Date.now(); };
        wrap.addEventListener('pointerup', endDrag); wrap.addEventListener('pointercancel', endDrag);
        wrap.addEventListener('wheel', (ev) => { if (this.hasAttribute('bare') && !ev.ctrlKey) return;   // in a chat turn the page scrolls, unless ctrl says otherwise
          ev.preventDefault(); const r = wrap.getBoundingClientRect();
          if (ev.shiftKey) { this._S.px -= (ev.deltaY || ev.deltaX); this._S.fit = false; this._place(); return; }   // shift: slide sideways, as a map does
          this._zoomBy(ev.deltaY < 0 ? 1.12 : 1 / 1.12, ev.clientX - r.left, ev.clientY - r.top); }, { passive: false });
        if (root.ResizeObserver) { this._ro = new ResizeObserver(() => this._schedule()); this._ro.observe(this); }
        this._schedule();
      }
      disconnectedCallback() { if (this._ro) { try { this._ro.disconnect(); } catch (_) {} } }
      _cardOf(id) { return this._last && this._last.cards.find((k) => k.id === id); }
      _zoomBy(f, ax, ay) { const wrap = this.querySelector('[data-r="wrap"]'); const r = wrap.getBoundingClientRect(); ax = ax == null ? r.width / 2 : ax; ay = ay == null ? r.height / 2 : ay; const z0 = this._S.zoom, z1 = Math.max(0.08, Math.min(4, z0 * f)); this._S.px = ax - (ax - this._S.px) * (z1 / z0); this._S.py = ay - (ay - this._S.py) * (z1 / z0); this._S.zoom = z1; this._S.fit = false; this._place(); }
      _place() { const v = this.querySelector('[data-r="view"]'), z = this.querySelector('[data-r="zoom"]'), sp = this.querySelector('[data-r="space"]'), wrap = this.querySelector('[data-r="wrap"]');
        if (v) v.style.transform = 'translate(' + px(this._S.px) + 'px,' + px(this._S.py) + 'px) scale(' + this._S.zoom.toFixed(3) + ')'; if (z) z.textContent = Math.round(this._S.zoom * 100) + '%';
        // the spacer gives the wrap its scroll extent: the scene at its scale plus the offset it stands at
        if (sp && this._last) { const o = this._last; sp.style.width = px(Math.max(0, this._S.px) + o.size.w * this._S.zoom + 8) + 'px'; sp.style.height = px(Math.max(0, this._S.py) + o.size.h * this._S.zoom + 8) + 'px'; }
        if (wrap && this._last) { const o = this._last; wrap.classList.toggle('scroll', o.size.w * this._S.zoom + 16 > wrap.clientWidth || o.size.h * this._S.zoom + 16 > wrap.clientHeight); } }
      _schedule() { if (this._raf || !this._built) return; this._raf = (root.requestAnimationFrame || setTimeout)(() => { this._raf = 0; this._render(); }); }
      _render() {
        if (!routesLib()) { ensureRoutes(this.ownerDocument, () => this._schedule()); return; }
        const S = this._S, $ = (r) => this.querySelector('[data-r="' + r + '"]'); const wrap = $('wrap'), view = $('view'); const W = wrap.clientWidth || 800, H = wrap.clientHeight || 500;
        if (!S.doc) { view.innerHTML = '<div class="sg-empty">nothing to explode yet</div>'; return; }
        let o; try { o = layout(S.doc, W, H, { layersOff: S.layersOff, mode: S.mode || undefined, direction: S.direction || undefined }); } catch (err) { view.innerHTML = '<div class="sg-empty">' + esc(err && err.message) + '</div>'; return; }
        this._last = o; view.innerHTML = sceneHtml(o);
        // FIT: scaled to the room, never below half (the exploded scene's rule for the embedded stage — text must stay
        // readable); a scene that does not fit at half scrolls instead, at half
        // in full mode the toolbar floats over the top of the wrap: the scene starts under it, so the verdict rail
        // and the first plate's caption are never covered
        const top = this.hasAttribute('bare') ? 8 : Math.max(34, (this.querySelector('.sg-ctl') || {}).offsetHeight || 0) + 16;   // the bar is empty on the first pass
        if (S.fit) { const z = Math.min(1, (W - 16) / Math.max(1, o.size.w), (H - top - 8) / Math.max(1, o.size.h));
          // fit to the room and let the reader zoom in — the old half-scale floor pushed a big scene off the stage
          // and (with the pan bug) stranded it there. 0.25 keeps text findable; the readout says where you are.
          S.zoom = Math.max(0.25, z); S.px = Math.max(8, (W - o.size.w * S.zoom) / 2); S.py = Math.max(top, (H - o.size.h * S.zoom) / 2); S.fit = false; }
        this._place(); this._paint();
        // the toolbar: the modes this kind of graph has, the layer chips with their counts
        // the chip names the mode you are IN. A flow contract holds a function's steps, not a file's symbols, so
        // it cannot be switched to dependency here -- that is a different explode, and the host asks for it.
        const modes = o.kind === 'code' ? (o.mode === 'flow' ? [['flow', 'Flow']] : [['dependency', 'Dependency']])
                                       : [['position', 'Position'], ['type', 'Type']];
        $('ctl').innerHTML = '<span class="c">explode</span>' + modes.map((m) => '<button data-m="' + m[0] + '"' + (o.mode === m[0] ? ' class="on"' : '') + '>' + m[1] + '</button>').join('') + '<span class="sep"></span><span class="c">layers</span>' +
          o.layers.map((l) => { const n = l.on ? (l.drawn || 0) : (l.hidden || 0);
            const why = l.on && l.hidden ? l.hidden + ' of ' + ((l.drawn || 0) + l.hidden) + ' hidden' + (l.hiddenBy && l.hiddenBy.length ? ' with ' + l.hiddenBy.join(' · ') : '') : '';
            return '<button class="sg-chip' + (l.on ? ' on' : ' off') + (why ? ' part' : '') + '" data-l="' + esc(l.id) + '" title="' + esc((l.by || '') + (l.where ? ' · ' + l.where : '') + (l.ms ? ' · ' + l.ms + ' ms' : '') + (why ? ' — ' + why : (l.on ? '' : ' — off; click to bring back ' + n))) + '" style="--lc:' + (l.kind === 'entity' ? 'var(--xp-ac)' : l.kind === 'relation' ? 'var(--xp-ac2)' : 'var(--xp-dv1)') + '"><i></i>' + esc(l.label) + '<b>' + (l.on ? '' : '+') + n + '</b>' + (why ? '<em class="hid" title="' + esc(why) + '">!</em>' : '') + '</button>'; }).join('');
        $('key').innerHTML = o.legend.length ? '<span class="c">runs</span>' + o.legend.map((l) => '<span class="' + l.cls + '" style="--kc:' + l.col + '"><i></i>' + esc(l.label) + '<b>' + l.n + '</b></span>').join('') + '<span class="dot" style="--kc:var(--xp-t3)"><i></i>heuristic</span><span style="--kc:var(--xp-t3);opacity:.5"><i></i>external</span>' : '';
        this.dispatchEvent(new CustomEvent('vera-explode-rendered', { detail: { cards: o.cards.length, edges: o.runs, size: o.size, mode: o.mode }, bubbles: true }));
      }
      // hover / selection: the card's runs lit, everything else quiet
      _paint() { const segs = this.querySelectorAll('.sg-e'), cards = this.querySelectorAll('.sg-card');
        // the host lit a part of the source (lightSpan): those cards lead, the rest are quiet — a reader's move in
        // the source answered in the diagram, and it outranks a stale hover
        const ext = this._extHit;
        if (ext && !this._hover) { segs.forEach((s) => { const hit = ext.ids.has(s.dataset.from) && ext.ids.has(s.dataset.to); s.classList.toggle('lit', hit); s.classList.toggle('dim', !hit); });
          cards.forEach((c) => { const hit = ext.ids.has(c.dataset.id); c.classList.toggle('lit', hit && c.dataset.id !== ext.sel); c.classList.toggle('dim', !hit); c.classList.toggle('sel', c.dataset.id === ext.sel); });
          if (ext.sel) { const el = this.querySelector('.sg-card[data-id="' + (window.CSS && CSS.escape ? CSS.escape(ext.sel) : ext.sel) + '"]'); if (el && el.scrollIntoView) try { el.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (_) {} }
          return; }
        const id = this._hover || this._S.sel; if (!id) { segs.forEach((s) => s.classList.remove('lit', 'dim')); cards.forEach((c) => { c.classList.remove('lit', 'dim'); c.classList.toggle('sel', c.dataset.id === this._S.sel); }); return; }
        const near = new Set([id]); segs.forEach((s) => { const hit = s.dataset.from === id || s.dataset.to === id; s.classList.toggle('lit', hit); s.classList.toggle('dim', !hit); if (hit) { near.add(s.dataset.from); near.add(s.dataset.to); } });
        cards.forEach((c) => { c.classList.toggle('lit', near.has(c.dataset.id) && c.dataset.id !== id); c.classList.toggle('dim', !near.has(c.dataset.id)); c.classList.toggle('sel', c.dataset.id === this._S.sel); }); }
    }
    root.customElements.define('vera-structgraph', VeraStructGraph);
  }
  const api = { layout, normalise, transpose, KIND, GLYPH, K, cardH, sceneHtml, ensureCss, ensureRoutes, version: 1 };
  root.VeraStructGraph = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
