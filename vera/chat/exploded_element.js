/* The exploded scene — the chat's Explode (UI redesign: the Chat & canvas set, Notes/40 §6 P6; the board's
   "ONE EXPLODED SCENE"): not three panes side by side but one pipeline for the session — per turn (a station) what
   the turn READ, the EXCHANGE, what it PRODUCED and where it LANDED on the canvas — with the runs drawn between the
   actual entities. Cards · Front · Iso are three projections of this one scene, not three re-implementations:
     cards — every station as a row of its layers, the cards in flow, the runs between them;
     front — the selected station as a carousel of its layers in depth (the same construction the board uses);
     iso   — the lattice: u = station, v = the layer bands, the plates identical parallelograms in a row, projected
             through the shared ISO projection (/ui/iso.js) when it is there, the classic 30°/45° otherwise.
   Nothing here uses CSS 3D except the front carousel; plates, cards and runs sit at screen coordinates from one
   projection, so a run ends on a card, not near it. The composer stays docked under the scene (the host's).

   <vera-exploded>  API: setScene({turns:[{mid, who, t, text, reply, read:[card], say:[card], made:[card],
                    land:[card]}], sel}) · mode(name) · select(mid) · fit() · state()
   card = {n, d, col, kind, body?, score?, p?, m?, rows?}
   events: vera:xpl:pick {mid, layer, card} · vera:xpl:turn {mid} · vera:xpl:rendered {mode, stations} · vera:xpl:place {mid}
   window.VeraExploded = { layout, LAYERS, version } — layout() is pure (node-testable).                        */
(function (root) {
  'use strict';
  const LAYERS = [
    { key: 'graph', name: 'context graph', sub: 'where it came from', col: 'var(--xp-dv1)', kind: 'graph' },
    { key: 'read', name: 'read', sub: 'what the turn read', col: 'var(--xp-dv1)' },
    { key: 'say', name: 'the exchange', sub: 'what it said', col: 'var(--xp-ac)' },
    { key: 'made', name: 'produced', sub: 'what it made', col: 'var(--xp-dv2)' },
    { key: 'land', name: 'session canvas', sub: 'where it landed', col: 'var(--xp-dv3)' }];
  const RAD = Math.PI / 180;
  const px = (v) => Math.round(v * 10) / 10;
  const isoP = (tilt, azim) => { const TH = tilt * RAD, AZ = azim * RAD; const sT = Math.sin(TH), cT = Math.cos(TH), cA = Math.cos(AZ), sA = Math.sin(AZ); return (u, v, z) => [u * cA - v * sA, (u * sA + v * cA) * sT - (z || 0) * cT]; };

  /* ── the layout, pure ─────────────────────────────────────────────────────────────────────────────── */
  function layout(scene, mode, W, H, o) {
    o = o || {}; const turns = (scene && scene.turns) || []; mode = mode === 'front' || mode === 'iso' ? mode : 'cards';
    const selIdx = Math.max(0, turns.findIndex((t) => t.mid === (scene && scene.sel))); const sel = turns.length ? Math.max(0, selIdx) : 0;
    const out = { mode, stations: turns.length, sel, plates: [], labels: [], cards: [], edges: [], panels: [], leaders: [], graphs: [], gnodes: [], size: { w: W, h: H }, fit: { s: 1, x: 0, y: 0 } };
    const den = o.den === 'hover' || o.den === 'zen' ? o.den : 'full';
    // an image travels with its card: Full gives it room on the card; Hover and Zen show it over the card instead
    const chOf = (c, CH) => (c && c.src && den === 'full') ? CH + 62 : CH;
    const cardsOf = (t, k) => (k === 'say' ? (t.say && t.say.length ? t.say : [{ n: t.text || '', d: (t.who || 'you') + (t.t ? ' · ' + t.t : ''), col: 'var(--xp-ac)', kind: 'note' }].concat(t.reply ? [{ n: t.reply, d: 'aide' + (t.rt ? ' · ' + t.rt : ''), col: 'var(--xp-ac)', kind: 'note' }] : [])) : (t[k] || []));
    const edge = (a, b, col, cls, title) => { const dx = b.x - a.x, dy = b.y - a.y; out.edges.push({ x: px(a.x), y: px(a.y), len: px(Math.sqrt(dx * dx + dy * dy)), deg: +(Math.atan2(dy, dx) * 180 / Math.PI).toFixed(2), col, cls: cls || '', title: title || '' }); };
    if (!turns.length) return out;
    if (mode === 'cards') {
      // every station a row: its layers as columns — the context graph first, then the cards in flow, the runs across the gutters
      const GUT = 26, PADX = 24, colW = Math.max(180, (W - PADX * 2 - GUT * (LAYERS.length - 1)) / LAYERS.length), CH = 54, CG = 8; let y = 24;
      turns.forEach((t, si) => {
        const gH = (t) => { const g = graphData(t); return g.nodes.length ? 24 + g.lanes * 26 : CH; };
        const colH = (L) => L.kind === 'graph' ? gH(t) + CG : cardsOf(t, L.key).reduce((s, c) => s + chOf(c, CH) + CG, 0);
        const rowH = 34 + Math.max.apply(null, LAYERS.map(colH).concat([CH + CG])) + 18;
        out.plates.push({ si, x: px(PADX - 10), y: px(y - 8), w: px(W - PADX * 2 + 20), h: px(rowH), cls: si === sel ? 'on' : '', mid: t.mid });
        out.labels.push({ si, x: px(PADX), y: px(y), n: (t.who || 'you') + ' · ' + (t.t || ''), k: String(t.text || '').slice(0, 60), cls: 'station' + (si === sel ? ' on' : ''), col: 'var(--xp-t2)', mid: t.mid });
        const pos = {};
        LAYERS.forEach((L, li) => {
          const x = PADX + li * (colW + GUT); const list = cardsOf(t, L.key);
          if (L.kind === 'graph') { const g = graphData(t); out.labels.push({ si, x: px(x), y: px(y + 18), n: L.name, k: String(g.nodes.length), cls: 'layer ' + L.key, col: L.col });
            if (g.nodes.length) out.graphs.push({ id: t.mid + ':graph', mid: t.mid, si, x: px(x), y: px(y + 34), w: px(colW), h: px(gH(t)), data: g }); pos[L.key] = []; return; }
          out.labels.push({ si, x: px(x), y: px(y + 18), n: L.name, k: String(list.length), cls: 'layer ' + L.key, col: L.col });
          let cy = y + 34;
          pos[L.key] = list.map((c, ci) => { const h = chOf(c, CH); const r = { x: x, y: cy, w: colW, h }; out.cards.push({ id: t.mid + ':' + L.key + ':' + ci, mid: t.mid, si, layer: L.key, ci, x: px(x), y: px(cy), w: px(colW), h: px(h), card: c, col: c.col || L.col }); cy += h + CG; return r; });
        });
        // the runs: every read into the exchange, the exchange into every product, every product to what landed
        const mid = (r) => ({ x: r.x + r.w, y: r.y + r.h / 2 }), left = (r) => ({ x: r.x, y: r.y + r.h / 2 });
        const ex = pos.say[0]; if (ex) { pos.read.forEach((r) => edge(mid(r), left(ex), 'var(--xp-dv1)', 'in', 'read by this turn')); pos.made.forEach((r) => edge(mid(ex), left(r), 'var(--xp-dv2)', 'out', 'produced by this turn')); }
        pos.made.forEach((r, i) => { const l = pos.land[i] || pos.land[0]; if (l) edge(mid(r), left(l), 'var(--xp-ac)', 'link', 'this became a canvas item'); });
        y += rowH + 14;
      });
      out.size = { w: W, h: y };
      return out;
    }
    if (mode === 'front') {
      // the selected station as a carousel of its layers in depth: the same construction the board uses
      const t = turns[sel]; const PDX = Math.max(300, Math.min(520, (W - 80) / 3.2)), PDZ = 126, li0 = o.layer == null ? 2 : o.layer;
      LAYERS.forEach((L, li) => { const off = li - li0, a = Math.abs(off); const list = cardsOf(t, L.key);
        out.panels.push({ layer: L.key, name: L.name, sub: L.sub, col: L.col, li, n: list.length, tf: 'translateX(' + (off * PDX).toFixed(0) + 'px) translateZ(' + (-a * PDZ).toFixed(0) + 'px) rotateY(26deg)', cls: (a === 0 ? 'on' : a === 1 ? 'near' : 'far') + (list.length > 5 ? ' many' : ''), d: (a * 0.07).toFixed(2) + 's', cards: list.map((c, ci) => ({ id: t.mid + ':' + L.key + ':' + ci, mid: t.mid, layer: L.key, ci, card: c, col: c.col || L.col })), graph: L.kind === 'graph' ? graphData(t) : null }); });
      for (let i = 0; i + 1 < LAYERS.length; i++) { const oa = i - li0, ob = i + 1 - li0; const ax = oa * PDX, az = -Math.abs(oa) * PDZ, bx = ob * PDX, bz = -Math.abs(ob) * PDZ; out.leaders.push({ tf: 'translateX(' + ax.toFixed(0) + 'px) translateZ(' + az.toFixed(0) + 'px) rotateY(' + (Math.atan2(-(bz - az), bx - ax) * 180 / Math.PI).toFixed(1) + 'deg)', w: Math.sqrt((bx - ax) * (bx - ax) + (bz - az) * (bz - az)).toFixed(0) + 'px' }); }
      out.station = { mid: t.mid, who: t.who, t: t.t, text: t.text };
      return out;
    }
    // iso: u = station, v = the layer bands; the plates identical parallelograms in a row (Stack: the stations on
    // floors, one above the other). Every band is drawn; every item is an iso widget standing on its pin.
    const P = o.proj || isoP(38, 45);   // a touch steeper than the classic 30°, so the rows down a band clear each other
    const STK = !!o.stack, WSZ = o.wsz === 's' || o.wsz === 'l' ? o.wsz : 'm';
    const VB = 230, PW = 400, PH = LAYERS.length * VB, pts = [];
    const FW = { s: 60, m: 78, l: 108 }[WSZ], FH = Math.round(FW * 0.56), STEM = 10, ROWMAX = 2, CWMIN = FW * 1.3, CAP = 6;   // six frames per band; the rest are pins with a count (the board pages past a plate's columns)
    const proj = (u, v, z) => { const p = P(u, v, z || 0); return { x: p[0], y: p[1] }; };
    out.bands = []; out.widgets = []; out.stack = STK; out.wsz = WSZ;
    // a plate is as wide as its fullest band needs: past three rows a band adds a column (the board: the plate grows along u)
    const widthOf = (t) => Math.max(PW, Math.max.apply(null, LAYERS.filter((L) => L.kind !== 'graph').map((L) => Math.ceil(Math.min(CAP, cardsOf(t, L.key).length) / ROWMAX) * CWMIN + 40)));
    const PWS = turns.map(widthOf); const PWmax = Math.max.apply(null, PWS);
    // a floor is held clear of the one beneath: the plate's projected depth plus a gap, in z (measured through P)
    const ZH = (() => { const a = proj(0, 0, 0), b = proj(PWmax, PH, 0), c = proj(0, 0, 100); const perZ = Math.max(0.05, (a.y - c.y) / 100); return Math.round((Math.abs(b.y - a.y) + 90) / perZ); })();
    let uRun = 0;
    turns.forEach((t, si) => {
      const PW = STK ? PWmax : PWS[si]; const u0 = STK ? 0 : uRun, z = STK ? (turns.length - 1 - si) * ZH : 0; if (!STK) uRun += PW + 80;
      const corners = [proj(u0, 0, z), proj(u0 + PW, 0, z), proj(u0 + PW, PH, z), proj(u0, PH, z)]; corners.forEach((c) => pts.push(c));
      out.plates.push({ si, mid: t.mid, cls: si === sel ? 'on' : '', poly: corners.map((c) => ({ x: c.x, y: c.y })), z });
      const lb = proj(u0, -18, z); out.labels.push({ si, x: lb.x, y: lb.y, n: (t.who || 'you') + ' · ' + (t.t || ''), k: String(t.text || '').slice(0, 40), cls: 'station' + (si === sel ? ' on' : ''), col: 'var(--xp-t2)', mid: t.mid });
      const pos = {};
      LAYERS.forEach((L, li) => {
        const v0 = li * VB; const list = cardsOf(t, L.key);
        // the band on the plate — drawn whether or not the turn has anything in it
        const bc = [proj(u0 + 4, v0 + 4, z), proj(u0 + PW - 4, v0 + 4, z), proj(u0 + PW - 4, v0 + VB - 4, z), proj(u0 + 4, v0 + VB - 4, z)];
        const ll = proj(u0 + PW + 8, v0 + 10, z);
        if (L.kind === 'graph') { const g = graphData(t);
          out.labels.push({ si, x: ll.x, y: ll.y, n: L.name, k: String(g.nodes.length), cls: 'layer sm ' + L.key, col: L.col });
          out.bands.push({ si, mid: t.mid, layer: L.key, poly: bc.map((c) => ({ x: c.x, y: c.y })), col: L.col, empty: !g.nodes.length, cls: si === sel ? 'on' : '' });
          // the context galaxy, iso view, lying in the band: the same widget the Context menu draws
          if (g.nodes.length) { const c = proj(u0 + PW / 2, v0 + VB / 2, z); pts.push(c); out.graphs.push({ id: t.mid + ':graph', mid: t.mid, si, x: c.x, y: c.y, w: 300, h: 150, data: g, iso: true }); }
          else { const e = proj(u0 + PW / 2, v0 + VB / 2, z); out.labels.push({ si, x: e.x, y: e.y, n: 'nothing read', k: '', cls: 'layer sm empty', col: 'var(--xp-t3)' }); }
          pos[L.key] = []; return; }
        out.labels.push({ si, x: ll.x, y: ll.y, n: L.name, k: String(list.length), cls: 'layer sm ' + L.key, col: L.col });
        out.bands.push({ si, mid: t.mid, layer: L.key, poly: bc.map((c) => ({ x: c.x, y: c.y })), col: L.col, empty: !list.length, cls: si === sel ? 'on' : '' });
        if (!list.length) { const e = proj(u0 + PW / 2, v0 + VB / 2, z); out.labels.push({ si, x: e.x, y: e.y, n: L.key === 'say' ? 'no reply yet' : L.key === 'read' ? 'nothing read' : L.key === 'made' ? 'nothing produced' : 'nothing landed', k: '', cls: 'layer sm empty', col: 'var(--xp-t3)' }); pos[L.key] = []; return; }
        // every item an iso widget on its pin: the band packs them in columns along the plate (2 · 3 · 4 across as
        // the count grows) and rows down the band, never past the band's own depth
        const shown = list.slice(0, CAP), more = list.length - shown.length;
        const RPC = Math.max(1, Math.min(Math.ceil(shown.length / ROWMAX), Math.floor((PW - 40) / CWMIN))), ROWS = Math.ceil(shown.length / RPC), CW = (PW - 40) / RPC, CV = ROWS > 1 ? (VB - 70) / (ROWS - 1) : 0;
        if (more > 0) { const mp = proj(u0 + PW - 30, v0 + VB - 26, z); out.labels.push({ si, x: mp.x, y: mp.y, n: '+' + more + ' more', k: 'in the graph band', cls: 'layer sm more', col: L.col, mid: t.mid }); }
        pos[L.key] = shown.map((c, ci) => { const p = proj(u0 + 20 + (ci % RPC) * CW + CW / 2, v0 + 40 + Math.floor(ci / RPC) * CV, z); pts.push(p);
          const wd = widgetOf(c); const id = t.mid + ':' + L.key + ':' + ci;
          out.widgets.push({ id, mid: t.mid, si, layer: L.key, ci, x: p.x, y: p.y, w: FW, h: FH, stem: STEM, card: c, col: c.col || L.col, form: wd.form, data: wd.data, placed: !!c.tpl, sample: !!wd.sample });
          out.cards.push({ id, mid: t.mid, si, layer: L.key, ci, x: p.x, y: p.y, w: FW, h: FH, card: c, col: c.col || L.col, anchored: true, iw: true });
          return p; });
      });
    });
    // fit the scene into the frame: scale and shift, widgets counter-scale in the DOM
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity; pts.forEach((p) => { x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y); x1 = Math.max(x1, p.x); y1 = Math.max(y1, p.y); });
    const s = Math.max(0.3, Math.min(1.4, Math.min((W - 80) / Math.max(1, x1 - x0 + 200), (H - 120) / Math.max(1, y1 - y0 + 140))));
    const dx = W / 2 - s * (x0 + x1) / 2, dy = H / 2 - s * (y0 + y1) / 2 + 20;
    out.fit = { s: +s.toFixed(3), x: px(dx), y: px(dy) };
    const T = (p) => ({ x: px(p.x * s + dx), y: px(p.y * s + dy) });
    out.plates.forEach((pl) => { pl.poly = pl.poly.map(T); });
    out.bands.forEach((b) => { b.poly = b.poly.map(T); });
    out.labels.forEach((l) => { const q = T(l); l.x = q.x; l.y = q.y; });
    out.cards.forEach((c) => { const q = T(c); c.x = q.x; c.y = q.y; });
    out.widgets.forEach((c) => { const q = T(c); c.x = q.x; c.y = q.y; });
    out.graphs.forEach((g) => { const q = T(g); g.x = q.x; g.y = q.y; });
    out.edges = []; // re-derive at screen scale: the runs between the widgets' pins
    turns.forEach((t) => { const byL = {}; out.widgets.filter((c) => c.mid === t.mid).forEach((c) => { (byL[c.layer] = byL[c.layer] || []).push(c); });
      const gr = out.graphs.find((g) => g.mid === t.mid);
      const ex = (byL.say || [])[0]; if (ex) { (byL.read || []).forEach((r) => edge(r, ex, 'var(--xp-dv1)', 'in', 'read by this turn')); if (gr && !(byL.read || []).length) edge(gr, ex, 'var(--xp-dv1)', 'in', 'the context this turn read'); (byL.made || []).forEach((r) => edge(ex, r, 'var(--xp-dv2)', 'out', 'produced by this turn')); }
      (byL.made || []).forEach((r, i) => { const l = (byL.land || [])[i] || (byL.land || [])[0]; if (l) edge(r, l, 'var(--xp-ac)', 'link', 'this became a canvas item'); }); });
    out.size = { w: W, h: H };
    return out;
  }
  /* ── an item as a WIDGET: the form and the data VeraWidget draws for a card of this kind ──────────────────── */
  const SAMPLE = { series: [3, 5, 4, 7, 6, 8, 7], level: { value: 62, max: 100 }, values: { a: 4, b: 7, c: 5, d: 6 }, items: [{ name: 'no reading yet', value: '' }], events: [{ t: '', text: 'no reading yet' }], stages: { steps: [{ label: 'no steps yet', status: '' }] }, string: 'no reading yet', points: [[1, 2], [2, 3], [3, 2]], graph: { nodes: [], links: [] } };
  const SHAPE = { trace: 'series', radial: 'level', counter: 'level', bar: 'level', bars: 'values', thermo: 'values', heat: 'values', matrix: 'values', donut: 'values', stack: 'values', pills: 'values', log: 'events', lane: 'events', table: 'items', files: 'items', list: 'items', checklist: 'items', stepper: 'stages', calendar: 'items', string: 'string', kv: 'values', pipes: 'graph', context_graph: 'graph', scatter: 'points' };
  function widgetOf(c) {
    c = c || {}; const k = String(c.kind || '').toLowerCase();
    if (c.form) return { form: c.form, data: c.data != null ? c.data : (SAMPLE[SHAPE[c.form] || 'string'] || SAMPLE.string), sample: c.data == null };
    if (k === 'widget') { const f = String(c.d || '').replace(/^widget\s*·\s*/, '').trim() || 'kv'; return { form: f, data: c.data != null ? c.data : (SAMPLE[SHAPE[f] || 'string'] || SAMPLE.string), sample: c.data == null }; }
    if (Array.isArray(c.steps) && c.steps.length) return { form: 'stepper', data: { steps: c.steps.map((s) => ({ label: s.label || s.n || '', status: s.status || '' })) } };
    if (Array.isArray(c.rows) && c.rows.length) return { form: 'kv', data: Object.fromEntries(c.rows.slice(0, 8).map((r) => [String(r.k), r.v])) };
    if (Array.isArray(c.bars) && c.bars.length) return { form: 'bars', data: Object.fromEntries(c.bars.map((b, i) => [String(i + 1), typeof b === 'number' ? b : parseFloat(b) || 0])) };
    if (k === 'diff') return { form: 'bars', data: { added: +c.p || 0, removed: +c.m || 0 } };
    // a record the turn read (a chunk, a memory, a dataset, an entity, a page…) stands as its relevance meter; its text is behind a click
    if (c.score != null && !/^(cap|capability|code|term|terminal|log|loop|image|widget|artifact|note)$/.test(k)) return { form: 'bar', data: { value: Math.round(Math.max(0, Math.min(1, +c.score)) * 100), max: 100 } };
    if (k === 'image') return { form: 'image', data: c.src || '' };
    if (k === 'code' || k === 'term' || k === 'terminal' || k === 'cap' || k === 'capability' || k === 'log') return { form: 'log', data: String(c.body || c.n || '').split('\n').filter(Boolean).slice(0, 8).map((l) => ({ t: '', text: l })) };
    if (k === 'loop') return { form: 'stepper', data: { steps: [{ label: c.d || 'running', status: 'run' }] } };
    return { form: 'string', data: String(c.body || c.n || '') };
  }
  // a frame: the widget drawn at the size; an image frame shows the picture; the registry's form when it is there
  function frameHtml(wg, H) {
    if (wg.form === 'image') return '<img class="xp-img" src="' + esc(wg.data) + '" alt="" loading="lazy" style="max-height:none;height:' + H + 'px;width:100%;margin:0;border-radius:0">';
    if (root.VeraWidget && typeof root.VeraWidget.draw === 'function') { try { return root.VeraWidget.draw(wg.form, wg.data, 'm', { height: H, bare: true, title: wg.card && wg.card.n }); } catch (_) {} }
    return '<div class="xp-ff">' + esc(String(typeof wg.data === 'string' ? wg.data : (wg.card && wg.card.n) || '')).slice(0, 120) + '</div>';
  }

  /* ── the context-graph layer's data, from the turn's cards: a lane per family, the relations the host recorded ── */
  const LANE_OF = (c) => { const k = String((c && (c.lane || c.kind || '')) || '').toLowerCase(); if (/memory|recall/.test(k)) return 'memory'; if (/loop|step|run/.test(k)) return 'loop'; if (/plan|goal/.test(k)) return 'plan'; if (/canvas|land|pin/.test(k)) return 'canvas'; return 'context'; };
  function graphData(t) {
    const nodes = []; const seen = {};
    const add = (c, lane) => { if (!c) return; const id = String(c.id || c.n || ''); if (!id || seen[id]) return; seen[id] = 1; nodes.push({ id, label: c.n || id, lane: lane || LANE_OF(c), kind: c.kind || '', score: +(c.score == null ? 0.5 : c.score), col: c.col || '', included: c.included !== false }); };
    // the graph sees everything the turn read (readAll — up to the 40 recorded), the read column only the first twelve
    (t.readAll || t.read || []).forEach((c) => add(c)); (t.land || []).forEach((c) => add(c, 'canvas'));
    (t.made || []).filter((c) => c && c.kind === 'loop' && Array.isArray(c.steps)).forEach((c) => c.steps.slice(0, 8).forEach((s, i) => add({ id: 'step:' + (i + 1), n: s.label || s.n || ('step ' + (i + 1)), kind: 'step', score: 1 - i * 0.08 }, 'loop')));
    const rels = (t.rel || []).filter((r) => r && seen[String(r.from)] && seen[String(r.to)]).map((r) => ({ from: String(r.from), to: String(r.to), kind: String(r.kind || 'cite') }));
    const laneList = ['context', 'memory', 'loop', 'plan', 'canvas'].filter((l) => nodes.some((n) => n.lane === l));
    return { nodes, rels, lanes: laneList.length, laneList };
  }
  // the mini graph is the registry's form (VeraWidget.draw 'context_graph'); a dots-only fallback when the widget element is not on the page
  function graphHtml(g, H) {
    if (root.VeraWidget && typeof root.VeraWidget.draw === 'function') { try { return root.VeraWidget.draw('context_graph', { nodes: g.nodes, rels: g.rels }, 'm', { height: H, bare: true, labels: true, full: false }); } catch (_) {} }
    const LH = Math.max(20, Math.floor(H / Math.max(1, g.lanes))); const idx = {}; g.laneList.forEach((l, i) => { idx[l] = i; });
    return '<div class="xp-gf" style="position:relative;height:' + H + 'px">' + g.nodes.map((n) => '<i title="' + esc(n.label) + '" style="left:' + (5 + (1 - n.score) * 82).toFixed(1) + '%;top:' + (2 + idx[n.lane] * LH + 9) + 'px;--cc:' + esc(n.col || 'var(--xp-dv1)') + '"></i>').join('') + '</div>';
  }

  const CSS = `
vera-exploded{display:flex;flex-direction:column;min-height:0;min-width:0;position:relative;overflow:hidden;--xp-bg:var(--bg0,#0e0f12);--xp-s1:var(--bg1,#15171c);--xp-s2:var(--bg2,#1b1e25);--xp-bd:var(--border,#2a2e37);--xp-t1:var(--fg,#e6e6e6);--xp-t2:var(--dim,#aaa);--xp-t3:var(--dim2,#777);--xp-ac:var(--acc,#7c9cff);--xp-ac2:var(--acc2,#5ec9a0);--xp-dv1:#a78bfa;--xp-dv2:#fb923c;--xp-dv3:#38bdf8;--xp-mono:var(--mono,ui-monospace,monospace);font-size:10.5px;color:var(--xp-t1);background:var(--xp-bg)}
vera-exploded .xp-ctl{position:absolute;left:14px;top:10px;z-index:30;display:flex;align-items:center;gap:5px;padding:5px 8px;border-radius:8px;background:color-mix(in srgb,var(--xp-s1) 90%,transparent);box-shadow:0 0 0 1px var(--xp-bd)}
vera-exploded .xp-ctl .c{font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--xp-t3);margin-right:3px}
vera-exploded .xp-ctl button{font:inherit;font-size:10px;color:var(--xp-t2);background:none;border:0;cursor:pointer;padding:3px 10px;border-radius:999px}
vera-exploded .xp-ctl button.on{background:var(--xp-ac);color:var(--xp-bg)}
vera-exploded .xp-ctl .sep{width:1px;height:14px;background:var(--xp-bd);margin:0 3px}
vera-exploded .xp-it .tpl{font-style:normal;color:var(--xp-ac);font-size:10px}
vera-exploded .xp-scrub{width:96px;accent-color:var(--xp-ac);margin:0 2px 0 6px;cursor:pointer}
vera-exploded .xp-g{position:absolute;z-index:8;border-radius:6px;background:var(--xp-s2);box-shadow:0 0 0 1px var(--xp-bd);padding:4px 6px;box-sizing:border-box;overflow:hidden}
vera-exploded .xp-gf i{position:absolute;width:9px;height:9px;border-radius:50%;transform:translate(-50%,-50%);box-shadow:inset 0 0 0 1.5px var(--cc);background:color-mix(in srgb,var(--cc) 32%,transparent)}
vera-exploded .xp-gn{position:absolute;width:9px;height:9px;border-radius:50%;transform:translate(-50%,-50%);box-shadow:inset 0 0 0 1.5px var(--cc);background:color-mix(in srgb,var(--cc) 32%,transparent);z-index:10}vera-exploded .xp-gn.ghost{background:transparent}
vera-exploded .xp-img{display:block;max-width:100%;max-height:56px;border-radius:4px;margin-top:3px;object-fit:cover}
vera-exploded[data-den="hover"] .xp-img,vera-exploded[data-den="zen"] .xp-img{display:none}
vera-exploded[data-den="hover"] .xp-it:hover .xp-img,vera-exploded[data-den="hover"] .xp-rc:hover .xp-img,vera-exploded[data-den="zen"] .xp-it.open .xp-img,vera-exploded[data-den="zen"] .xp-rc.open .xp-img{display:block;position:absolute;left:0;top:100%;z-index:40;max-height:220px;max-width:280px;box-shadow:0 6px 18px rgba(0,0,0,.5)}
vera-exploded[data-den="hover"] .xp-it.has-img,vera-exploded[data-den="zen"] .xp-it.has-img{overflow:visible}
vera-exploded[data-den="hover"] .xp-iw:hover .xp-img,vera-exploded[data-den="zen"] .xp-iw.open .xp-img{display:block}
vera-exploded[data-den="hover"] .xp-iw .xp-if.img .xp-if-bd,vera-exploded[data-den="zen"] .xp-iw .xp-if.img .xp-if-bd{display:flex;align-items:center;justify-content:center;font-family:var(--xp-mono);font-size:8.5px;color:var(--xp-t3)}
vera-exploded[data-den="hover"] .xp-iw .xp-if.img .xp-if-bd::before{content:'image · hover'}vera-exploded[data-den="zen"] .xp-iw .xp-if.img .xp-if-bd::before{content:'image · click'}
vera-exploded[data-den="hover"] .xp-iw:hover .xp-if.img .xp-if-bd::before,vera-exploded[data-den="zen"] .xp-iw.open .xp-if.img .xp-if-bd::before{content:none}
vera-exploded .xp-wrap{flex:1;min-height:0;position:relative;overflow:hidden;cursor:grab;touch-action:none;user-select:none}
vera-exploded .xp-wrap.dragging{cursor:grabbing}vera-exploded .xp-wrap.dragging .xp-view{transition:none}
vera-exploded .xp-view{position:absolute;inset:0;transform-origin:50% 50%;transition:transform .38s cubic-bezier(.22,.68,.18,1)}
vera-exploded .xp-view.scroll{overflow:auto;transition:none}
vera-exploded .xp-pl{position:absolute;pointer-events:none;background:color-mix(in srgb,var(--xp-ac) 5%,transparent);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--xp-ac) 20%,transparent);border-radius:8px}
vera-exploded .xp-pl.on{background:color-mix(in srgb,var(--xp-ac) 9%,transparent);box-shadow:inset 0 0 0 1.5px color-mix(in srgb,var(--xp-ac) 45%,transparent)}
vera-exploded .xp-pl.iso{border-radius:0;clip-path:var(--cp);box-shadow:none;background:color-mix(in srgb,var(--xp-ac) 8%,transparent)}
vera-exploded .xp-lb{position:absolute;font-size:10px;letter-spacing:.12em;text-transform:uppercase;white-space:nowrap;display:flex;gap:8px;align-items:baseline;pointer-events:none;color:var(--xp-t3)}
vera-exploded .xp-lb b{font-family:var(--xp-mono);font-size:9.5px;letter-spacing:0;font-weight:400;text-transform:none;color:var(--xp-t3);max-width:320px;overflow:hidden;text-overflow:ellipsis}
vera-exploded .xp-lb.station{font-size:11px;color:var(--xp-t2);pointer-events:auto;cursor:pointer}vera-exploded .xp-lb.station.on{color:var(--xp-t1)}vera-exploded .xp-lb.station:hover{text-decoration:underline;text-underline-offset:3px}
vera-exploded .xp-lb.sm{font-size:8.5px}
vera-exploded .xp-e{position:absolute;height:2px;transform-origin:0 50%;z-index:6;pointer-events:none;border-radius:2px;background:var(--ec);opacity:.75;box-shadow:0 0 7px -2px var(--ec)}
vera-exploded .xp-e.link{height:0;border-top:2px dashed var(--ec);background:none;box-shadow:none}
vera-exploded .xp-it{position:absolute;border-radius:6px;background:var(--xp-s2);box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-bd);cursor:pointer;z-index:10;padding:6px 10px;display:flex;flex-direction:column;gap:2px;box-sizing:border-box;overflow:hidden;transition:box-shadow .15s ease}
vera-exploded .xp-it:hover{box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-t3),0 14px 28px -18px rgba(0,0,0,.95);z-index:22}
vera-exploded .xp-it.open{height:auto!important;z-index:26;background:color-mix(in srgb,var(--xp-s1) 97%,transparent);box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1.5px var(--xp-ac),0 26px 46px -18px rgba(0,0,0,.98)}
vera-exploded .xp-it.anch{transform:translate(-50%,-50%) scale(var(--inv,1));transform-origin:50% 50%}vera-exploded .xp-it.anch.open{transform:translate(-50%,-50%) scale(var(--inv,1))}
vera-exploded .xp-it .n{font-size:11px;color:var(--xp-t1);line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex-shrink:0}vera-exploded .xp-it.open .n{white-space:normal}
vera-exploded .xp-it .d{font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex-shrink:0}
vera-exploded .xp-it .b{display:none;margin-top:4px;font-size:10px;color:var(--xp-t2);line-height:1.4}vera-exploded .xp-it.open .b{display:block;max-height:170px;overflow:auto}
vera-exploded .xp-it .b pre{margin:0;font-family:var(--xp-mono);font-size:9px;white-space:pre-wrap;color:var(--xp-t2)}
vera-exploded .xp-it .b .bar{height:5px;border-radius:3px;background:var(--xp-bd);overflow:hidden}vera-exploded .xp-it .b .bar i{display:block;height:100%;background:var(--cc)}
vera-exploded .xp-it .b .pm b{font-family:var(--xp-mono);margin-right:8px}vera-exploded .xp-it .b .pm .p{color:var(--xp-ac2)}vera-exploded .xp-it .b .pm .m{color:#e06c75}
vera-exploded .xp-it .b .kv{display:grid;grid-template-columns:auto 1fr;gap:1px 8px;font-family:var(--xp-mono);font-size:9px}vera-exploded .xp-it .b .kv b{color:var(--xp-t3);font-weight:400}
vera-exploded .xp-it .b .steps span{display:flex;gap:6px;font-family:var(--xp-mono);font-size:9px}vera-exploded .xp-it .b .steps i{width:6px;height:6px;border-radius:50%;background:var(--xp-t3);align-self:center;flex-shrink:0}vera-exploded .xp-it .b .steps span.ok i{background:var(--xp-ac2)}vera-exploded .xp-it .b .steps span.run i{background:var(--xp-ac)}vera-exploded .xp-it .b .steps span.fail i{background:#e06c75}
vera-exploded .xp-dots{position:absolute;right:12px;top:10px;z-index:32;display:flex;flex-direction:column;align-items:flex-end;gap:6px;padding:8px 7px;border-radius:8px;max-height:calc(100% - 40px);overflow:hidden}
vera-exploded .xp-dots:hover{background:color-mix(in srgb,var(--xp-s1) 92%,transparent);box-shadow:0 0 0 1px var(--xp-bd)}
vera-exploded .xp-dot{display:flex;align-items:center;gap:8px;cursor:pointer;height:12px}vera-exploded .xp-dot i{width:6px;height:6px;border-radius:2px;background:var(--xp-t3);opacity:.5;flex-shrink:0;order:2}vera-exploded .xp-dot:hover i{opacity:1}vera-exploded .xp-dot.on i{opacity:1;background:var(--xp-ac);box-shadow:0 0 0 3px color-mix(in srgb,var(--xp-ac) 22%,transparent)}
vera-exploded .xp-dot .l{order:1;display:none;font-family:var(--xp-mono);font-size:9px;color:var(--xp-t2);white-space:nowrap;max-width:220px;overflow:hidden;text-overflow:ellipsis}vera-exploded .xp-dots:hover .xp-dot .l{display:block}
vera-exploded .xp-pz{position:absolute;left:14px;bottom:12px;z-index:34;display:flex;align-items:center;gap:3px;padding:4px 6px;border-radius:8px;background:color-mix(in srgb,var(--xp-s1) 90%,transparent);box-shadow:0 0 0 1px var(--xp-bd);opacity:.6}vera-exploded .xp-pz:hover{opacity:1}
vera-exploded .xp-pz button{font:inherit;font-family:var(--xp-mono);font-size:11px;color:var(--xp-t2);background:none;border:0;cursor:pointer;padding:3px 7px;border-radius:4px}vera-exploded .xp-pz button:hover{background:var(--xp-s2);color:var(--xp-t1)}vera-exploded .xp-pz .z{font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3);min-width:34px;text-align:center}
/* FRONT: the carousel */
vera-exploded .xp-car{position:absolute;inset:0;perspective:2800px;perspective-origin:50% 48%;overflow:hidden}
vera-exploded .xp-track{position:absolute;left:50%;top:50%;width:0;height:0;transform-style:preserve-3d;transition:transform .5s cubic-bezier(.2,.85,.25,1.02)}
vera-exploded .xp-cp{position:absolute;left:-200px;top:-236px;width:400px;height:472px;border-radius:8px;cursor:pointer;display:flex;flex-direction:column;gap:9px;padding:14px;box-sizing:border-box;background:color-mix(in srgb,var(--pc) 8%,var(--xp-s1));box-shadow:0 0 0 1px color-mix(in srgb,var(--pc) 32%,transparent),0 12px 30px -14px rgba(0,0,0,.9);transition:transform .64s cubic-bezier(.2,.85,.25,1.04),opacity .46s ease;animation:xp-cp-in .62s cubic-bezier(.2,.8,.25,1) backwards;animation-delay:var(--d,0s)}
@keyframes xp-cp-in{from{opacity:0;transform:translateX(0) translateZ(-140px) rotateY(-6deg)}}
vera-exploded .xp-cp.on{background:var(--xp-s1);box-shadow:0 0 0 1.5px color-mix(in srgb,var(--pc) 62%,transparent),0 22px 40px -26px #000}vera-exploded .xp-cp.near{opacity:.92}vera-exploded .xp-cp.far{opacity:.55}
vera-exploded .xp-cp-h{display:flex;align-items:baseline;gap:7px;font-size:9px;letter-spacing:.15em;text-transform:uppercase;color:color-mix(in srgb,var(--pc) 90%,var(--xp-t3));flex-shrink:0;cursor:pointer}vera-exploded .xp-cp-h i{width:7px;height:7px;border-radius:2px;background:var(--pc);align-self:center}vera-exploded .xp-cp-h b{font-family:var(--xp-mono);font-size:9px;letter-spacing:0;color:var(--xp-t3);font-weight:400;text-transform:none;margin-left:auto}
vera-exploded .xp-cp-b{flex:1;min-height:0;display:flex;flex-direction:column;gap:10px;overflow:auto;padding:0 6px 2px}
vera-exploded .xp-cp.many .xp-cp-b{display:grid;grid-template-columns:1fr 1fr;align-content:start}
vera-exploded .xp-rc{position:relative;display:flex;flex-direction:column;gap:2px;padding:8px 10px;border-radius:6px;background:var(--xp-s2);cursor:pointer;box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-bd)}vera-exploded .xp-rc:hover{box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-t3)}vera-exploded .xp-rc.open{box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-ac)}
vera-exploded .xp-rc .n{font-size:11px;color:var(--xp-t1)}vera-exploded .xp-rc .d{font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3)}vera-exploded .xp-rc .b{display:none;margin-top:4px;font-size:10px;color:var(--xp-t2)}vera-exploded .xp-rc.open .b{display:block}vera-exploded .xp-rc .b pre{margin:0;font-family:var(--xp-mono);font-size:9px;white-space:pre-wrap}
vera-exploded .xp-spine{position:absolute;left:-2600px;top:-1px;width:5200px;height:2px;pointer-events:none;background:linear-gradient(90deg,transparent,color-mix(in srgb,var(--xp-t3) 26%,transparent) 20%,color-mix(in srgb,var(--xp-t3) 26%,transparent) 80%,transparent)}
vera-exploded .xp-lead{position:absolute;left:0;top:0;height:0;transform-origin:0 50%;border-top:1px dashed var(--xp-bd);pointer-events:none}
vera-exploded .xp-nav{position:absolute;top:50%;transform:translateY(-50%);z-index:30;width:30px;height:52px;border:0;border-radius:6px;cursor:pointer;font-size:17px;line-height:1;color:var(--xp-t2);background:color-mix(in srgb,var(--xp-s1) 84%,transparent);box-shadow:0 0 0 1px var(--xp-bd)}vera-exploded .xp-nav:hover{color:var(--xp-t1);background:var(--xp-s2)}vera-exploded .xp-nav.l{left:16px}vera-exploded .xp-nav.r{right:16px}
/* ISO WIDGETS: a frame standing on a stem above its pin, the caption beneath — the board's "an iso widget standing on the plate, no frame: the card is only its label" */
vera-exploded .xp-iw{position:absolute;width:0;height:0;z-index:12;transform:scale(var(--inv,1));transform-origin:0 0;cursor:pointer}
vera-exploded .xp-iw .xp-stem{position:absolute;left:0;bottom:0;width:1px;background:color-mix(in srgb,var(--cc) 70%,transparent);transform:translateX(-.5px)}
vera-exploded .xp-iw .xp-stem::after{content:'';position:absolute;left:-2.5px;bottom:-1.5px;width:6px;height:3px;border-radius:50%;background:var(--cc)}
vera-exploded .xp-if{position:absolute;left:0;transform:translateX(-50%);background:var(--xp-s1);border-radius:3px;box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--cc) 45%,var(--xp-bd)),0 10px 22px -12px rgba(0,0,0,.9);overflow:hidden;display:flex;flex-direction:column;box-sizing:border-box;transition:box-shadow .15s}
vera-exploded .xp-iw:hover .xp-if{box-shadow:inset 0 0 0 1px var(--cc),0 14px 28px -12px rgba(0,0,0,.95)}
vera-exploded .xp-iw.open .xp-if{box-shadow:inset 0 0 0 1.5px var(--xp-ac),0 20px 40px -14px #000}
vera-exploded .xp-if-hd{flex-shrink:0;display:flex;align-items:center;gap:3px;height:13px;padding:0 5px;background:var(--xp-s2);color:var(--xp-t3);font-family:var(--xp-mono);font-size:7.5px}
vera-exploded .xp-if-hd i{width:4px;height:4px;border-radius:50%;background:var(--xp-bd);flex-shrink:0}vera-exploded .xp-if-hd i:nth-child(1){background:#e06c75}vera-exploded .xp-if-hd i:nth-child(2){background:var(--xp-dv2)}vera-exploded .xp-if-hd i:nth-child(3){background:var(--xp-ac2)}
vera-exploded .xp-if-hd span{margin-left:3px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;color:var(--xp-t2)}vera-exploded .xp-if-hd .tpl{margin-left:auto;color:var(--xp-ac);font-style:normal}
vera-exploded .xp-if-bd{flex:1;min-height:0;padding:4px 6px;overflow:hidden;font-size:9px;color:var(--xp-t2)}
vera-exploded .xp-if-bd .vw-svg{height:100%!important}vera-exploded .xp-if-bd .wempty{font-size:8.5px}vera-exploded .xp-if-bd .xp-ff{font-family:var(--xp-mono);font-size:8.5px;line-height:1.35;white-space:pre-wrap;color:var(--xp-t2)}
vera-exploded .xp-if.sample .xp-if-bd{opacity:.6}
vera-exploded .xp-cap{position:absolute;left:0;top:6px;transform:translateX(-50%);width:max-content;max-width:190px;display:flex;flex-direction:column;align-items:center;gap:1px;text-align:center;pointer-events:none}
vera-exploded .xp-cap .n{font-size:10.5px;color:var(--xp-t1);line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:190px;text-shadow:0 1px 3px var(--xp-bg)}
vera-exploded .xp-cap .d{font-family:var(--xp-mono);font-size:8.5px;color:var(--xp-t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:190px}
vera-exploded .xp-iw .b{display:none}vera-exploded .xp-iw.open .b{display:block;position:absolute;left:0;top:34px;transform:translateX(-50%);width:230px;max-height:170px;overflow:auto;background:color-mix(in srgb,var(--xp-s1) 97%,transparent);box-shadow:0 0 0 1px var(--xp-ac),0 20px 40px -14px #000;border-radius:6px;padding:8px 10px;font-size:10px;color:var(--xp-t2);line-height:1.4;z-index:30;text-align:left}
vera-exploded .xp-iw .b pre{margin:0;font-family:var(--xp-mono);font-size:9px;white-space:pre-wrap}vera-exploded .xp-iw .b .kv{display:grid;grid-template-columns:auto 1fr;gap:1px 8px;font-family:var(--xp-mono);font-size:9px}vera-exploded .xp-iw .b .kv b{color:var(--xp-t3);font-weight:400}
vera-exploded .xp-iw .b .steps span{display:flex;gap:6px;font-family:var(--xp-mono);font-size:9px}vera-exploded .xp-iw .b .steps i{width:6px;height:6px;border-radius:50%;background:var(--xp-t3);align-self:center;flex-shrink:0}
vera-exploded .xp-iw .b .bar{height:5px;border-radius:3px;background:var(--xp-bd);overflow:hidden}vera-exploded .xp-iw .b .bar i{display:block;height:100%;background:var(--cc)}
/* the bands: every layer drawn on the plate, a faint strip; the empty ones dashed */
vera-exploded .xp-band{position:absolute;pointer-events:none;clip-path:var(--cp);background:color-mix(in srgb,var(--bc) 7%,transparent)}
vera-exploded .xp-band.empty{background:repeating-linear-gradient(135deg,color-mix(in srgb,var(--bc) 6%,transparent) 0 6px,transparent 6px 14px)}
vera-exploded .xp-band.on{background:color-mix(in srgb,var(--bc) 11%,transparent)}
vera-exploded .xp-lb.more{font-size:8.5px;letter-spacing:.04em;text-transform:none;transform:translate(-100%,-50%);pointer-events:auto;cursor:pointer}
vera-exploded .xp-lb.empty{font-size:8.5px;letter-spacing:.06em;text-transform:none;font-style:italic;opacity:.7;transform:translate(-50%,-50%)}
/* the galaxy lying on the plate */
vera-exploded .xp-g.iso{background:transparent;box-shadow:none;transform:translate(-50%,-50%) scale(var(--inv,1));transform-origin:50% 50%;padding:0;overflow:visible;z-index:9}
/* Blocks off: plates, bands and frames lose their fills — outlines only (the board) */
:root[data-blocks="off"] vera-exploded .xp-pl,:root[data-blocks="off"] vera-exploded .xp-band{background:none!important}
:root[data-blocks="off"] vera-exploded .xp-pl.iso{box-shadow:none;outline:1px solid color-mix(in srgb,var(--xp-ac) 30%,transparent);outline-offset:-1px}
:root[data-blocks="off"] vera-exploded .xp-if,:root[data-blocks="off"] vera-exploded .xp-it,:root[data-blocks="off"] vera-exploded .xp-cp{background:transparent!important}
:root[data-blocks="off"] vera-exploded .xp-if-hd{background:transparent}
/* the scene tips in when it opens and flattens back when it closes (3D → 2D), the transcript fading around it */
vera-exploded .xp-wrap{perspective:1400px}
vera-exploded.opening .xp-view{animation:xp-tip .5s cubic-bezier(.2,.8,.2,1) both}
vera-exploded.closing .xp-view{animation:xp-flat .24s ease-in both}
@keyframes xp-tip{from{opacity:0;transform:rotateX(-26deg) scale(.94)}to{opacity:1;transform:none}}
@keyframes xp-flat{from{opacity:1;transform:none}to{opacity:0;transform:rotateX(-26deg) scale(.96)}}
vera-exploded .xp-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--xp-t3);text-align:center;padding:20px;line-height:1.5}
`;
  function ensureCss(doc) { doc = doc || document; if (doc.getElementById('vera-exploded-css')) return; const s = doc.createElement('style'); s.id = 'vera-exploded-css'; s.textContent = CSS; (doc.head || doc.documentElement).appendChild(s); }
  const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  function cardBody(c) {
    const k = c.kind || ''; let h = '';
    if (c.src) h += '<img class="xp-img" src="' + esc(c.src) + '" alt="" loading="lazy">';
    if (c.score != null) h += '<div class="bar"><i style="width:' + Math.round(Math.max(0, Math.min(1, +c.score)) * 100) + '%"></i></div>';
    if (k === 'diff' && (c.p != null || c.m != null)) h += '<div class="pm"><b class="p">+' + esc(c.p || 0) + '</b><b class="m">−' + esc(c.m || 0) + '</b></div>';
    if (Array.isArray(c.rows) && c.rows.length) h += '<div class="kv">' + c.rows.slice(0, 8).map((r) => '<b>' + esc(r.k) + '</b><span>' + esc(r.v) + '</span>').join('') + '</div>';
    if (Array.isArray(c.steps) && c.steps.length) h += '<div class="steps">' + c.steps.slice(0, 12).map((s) => '<span class="' + esc(s.status || '') + '"><i></i>' + esc(s.label || s.n || '') + (s.cap ? ' · ' + esc(s.cap) : '') + '</span>').join('') + '</div>';
    if (c.body) h += '<pre>' + esc(String(c.body).slice(0, 600)) + '</pre>';
    return h;
  }

  if (typeof HTMLElement !== 'undefined' && root.customElements && !root.customElements.get('vera-exploded')) {
    class VeraExploded extends HTMLElement {
      constructor() { super(); this._S = { scene: { turns: [], sel: '' }, mode: 'cards', layer: 2, open: null, pan: { x: 0, y: 0, z: 1 } }; this._raf = 0; }
      connectedCallback() {
        ensureCss(this.ownerDocument); if (this._built) { this._schedule(); return; } this._built = true;
        const m = this.getAttribute('mode'); if (m) this._S.mode = m;
        this.innerHTML = '<div class="xp-ctl"><span class="c">explode</span><button data-m="cards">Cards</button><button data-m="front">Front</button><button data-m="iso">Iso</button><span class="sep"></span><button data-a="fit" title="Back to the whole scene">Fit</button><button data-a="close" title="Back to the flat transcript">Flatten</button><span class="sep"></span><button data-a="stack" title="Stack — the stations on floors, one above the other">Stack</button><button data-a="wsz" title="The iso widgets\' size — S · M · L">M</button><span class="sep"></span><button data-a="place" title="Place a widget from the registry onto this station\'s plate — it becomes one of the turn\'s items, tagged ⧉ with its template">+ Place</button><span class="sep"></span><input type="range" class="xp-scrub" data-r="scrub" min="0" max="0" value="0" title="Scrub through the session\'s turns (← → too)"></div><div class="xp-dots" data-r="dots"></div><div class="xp-wrap" data-r="wrap"><div class="xp-view" data-r="view"></div></div><div class="xp-pz"><button data-a="zout">−</button><span class="z" data-r="zoom">100%</span><button data-a="zin">+</button></div>';
        this._r = {}; this.querySelectorAll('[data-r]').forEach((el) => { this._r[el.dataset.r] = el; });
        this.addEventListener('click', (e) => this._click(e));
        // the timeline: the slider and ← → walk the session's turns
        if (!this.hasAttribute('tabindex')) this.setAttribute('tabindex', '0');
        if (this._r.scrub) this._r.scrub.addEventListener('input', () => { const ts = this._S.scene.turns || []; const t = ts[Math.max(0, Math.min(ts.length - 1, +this._r.scrub.value || 0))]; if (t && t.mid !== this._S.scene.sel) this.select(t.mid); });
        this.addEventListener('keydown', (ev) => { if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return; if (ev.target && /^(input|textarea)$/i.test(ev.target.tagName)) return; const ts = this._S.scene.turns || []; if (!ts.length) return; let i = ts.findIndex((t) => t.mid === this._S.scene.sel); i = Math.max(0, Math.min(ts.length - 1, i + (ev.key === 'ArrowRight' ? 1 : -1))); this.select(ts[i].mid); ev.preventDefault(); });
        const wrap = this._r.wrap;
        wrap.addEventListener('wheel', (e) => { if (this._S.mode === 'cards') return; e.preventDefault(); const p = this._S.pan; const nz = Math.max(0.4, Math.min(3, p.z * (e.deltaY > 0 ? 0.9 : 1.12))); const r = wrap.getBoundingClientRect(); const qx = e.clientX - (r.left + r.width / 2), qy = e.clientY - (r.top + r.height / 2); const k = nz / p.z; this._S.pan = { z: nz, x: qx - (qx - p.x) * k, y: qy - (qy - p.y) * k }; this._applyPan(); }, { passive: false });
        wrap.addEventListener('pointerdown', (e) => { if (e.button || this._S.mode === 'cards' || (e.target.closest && e.target.closest('.xp-it,.xp-rc,.xp-cp,button,.xp-lb.station'))) return; this._drag = { x0: e.clientX, y0: e.clientY, px: this._S.pan.x, py: this._S.pan.y, id: e.pointerId, moved: false }; });
        wrap.addEventListener('pointermove', (e) => { const g = this._drag; if (!g) return; const dx = e.clientX - g.x0, dy = e.clientY - g.y0; if (!g.moved && Math.abs(dx) + Math.abs(dy) > 4) { g.moved = true; wrap.classList.add('dragging'); try { wrap.setPointerCapture(g.id); } catch (_) {} } if (g.moved) { this._S.pan.x = g.px + dx; this._S.pan.y = g.py + dy; this._applyPan(); } });
        const up = () => { if (this._drag) { wrap.classList.remove('dragging'); this._drag = null; } }; wrap.addEventListener('pointerup', up); wrap.addEventListener('pointercancel', up);
        if (root.ResizeObserver) { this._ro = new ResizeObserver(() => this._schedule()); this._ro.observe(wrap); }
        this._schedule();
      }
      disconnectedCallback() { if (this._ro) { try { this._ro.disconnect(); } catch (_) {} } }
      tipIn() { this.classList.remove('closing'); this.classList.add('opening'); clearTimeout(this._tipT); this._tipT = setTimeout(() => this.classList.remove('opening'), 600); }
      flatten(done) { this.classList.remove('opening'); this.classList.add('closing'); clearTimeout(this._tipT); this._tipT = setTimeout(() => { this.classList.remove('closing'); if (done) { try { done(); } catch (_) {} } }, 260); }
      setScene(scene) { this._S.scene = scene && scene.turns ? scene : { turns: [], sel: '' }; if (!this._S.scene.sel && this._S.scene.turns.length) this._S.scene.sel = this._S.scene.turns[this._S.scene.turns.length - 1].mid; this._schedule(); }
      stack(on) { this._S.stack = on == null ? !this._S.stack : !!on; this._S.pan = { x: 0, y: 0, z: 1 }; this._schedule(); return this._S.stack; }
      widgetSize(s) { const L = ['s', 'm', 'l']; this._S.wsz = L.includes(s) ? s : L[(L.indexOf(this._S.wsz || 'm') + 1) % L.length]; this._schedule(); return this._S.wsz; }
      mode(name) { if (name && /^(cards|front|iso)$/.test(name)) { this._S.mode = name; this._S.pan = { x: 0, y: 0, z: 1 }; this._S.open = null; this._schedule(); } return this._S.mode; }
      select(mid) { this._S.scene.sel = mid; this._schedule(); this.dispatchEvent(new CustomEvent('vera:xpl:turn', { detail: { mid }, bubbles: true })); }
      fit() { this._S.pan = { x: 0, y: 0, z: 1 }; this._applyPan(); }
      state() { return this._S; }
      _applyPan() {
        this.querySelectorAll('.xp-iw,.xp-g.iso').forEach((el) => { const k = Math.max(0.5, Math.min(2.2, (this._last ? this._last.fit.s : 1) * this._S.pan.z)); el.style.setProperty('--inv', Math.pow(k, -0.3).toFixed(3)); }); const p = this._S.pan; if (this._r.view) this._r.view.style.transform = this._S.mode === 'cards' ? 'none' : 'translate(' + p.x + 'px,' + p.y + 'px) scale(' + p.z + ')'; if (this._r.zoom) this._r.zoom.textContent = Math.round(p.z * 100) + '%'; this.querySelectorAll('.xp-it.anch').forEach((el) => { el.style.setProperty('--inv', (1 / Math.max(0.5, Math.min(2.2, (this._last ? this._last.fit.s : 1) * p.z))).toFixed(3)); }); }
      _click(e) {
        const t = e.target; const mb = t.closest && t.closest('button[data-m]'); if (mb) { this.mode(mb.dataset.m); return; }
        const ab = t.closest && t.closest('[data-a]'); if (ab) { const k = ab.dataset.a; if (k === 'fit') this.fit(); else if (k === 'stack') this.stack(); else if (k === 'wsz') this.widgetSize(); else if (k === 'place') { const ts = this._S.scene.turns || []; const t = ts.find((x) => x.mid === this._S.scene.sel) || ts[ts.length - 1]; this.dispatchEvent(new CustomEvent('vera:xpl:place', { detail: { mid: t ? t.mid : '' }, bubbles: true })); } else if (k === 'close') this.dispatchEvent(new CustomEvent('vera:xpl:close', { bubbles: true })); else if (k === 'zin' || k === 'zout') { const p = this._S.pan; p.z = Math.max(0.4, Math.min(3, p.z * (k === 'zin' ? 1.2 : 0.83))); this._applyPan(); } else if (k === 'prev' || k === 'next') { this._S.layer = Math.max(0, Math.min(LAYERS.length - 1, this._S.layer + (k === 'next' ? 1 : -1))); this._schedule(); } return; }
        const dot = t.closest && t.closest('.xp-dot'); if (dot) { this.select(dot.dataset.mid); return; }
        const st = t.closest && t.closest('.xp-lb.station'); if (st) { this.select(st.dataset.mid); return; }
        const ph = t.closest && t.closest('.xp-cp-h'); if (ph) { this._S.layer = +ph.closest('.xp-cp').dataset.li; this._schedule(); return; }
        const card = t.closest && t.closest('.xp-it,.xp-rc,.xp-iw'); if (card) { const id = card.dataset.id; this._S.open = this._S.open === id ? null : id; const [mid, layer] = id.split(':'); this._schedule(); this.dispatchEvent(new CustomEvent('vera:xpl:pick', { detail: { mid, layer, card: id }, bubbles: true })); if (layer === 'say') this.dispatchEvent(new CustomEvent('vera:xpl:turn', { detail: { mid }, bubbles: true })); return; }
        const cp = t.closest && t.closest('.xp-cp'); if (cp) { this._S.layer = +cp.dataset.li; this._schedule(); }
      }
      _schedule() { if (this._raf || !this._built) return; this._raf = (root.requestAnimationFrame || setTimeout)(() => { this._raf = 0; this._render(); }); }
      _render() {
        const S = this._S, wrap = this._r.wrap, view = this._r.view; const W = wrap.clientWidth || 800, H = wrap.clientHeight || 600;
        const proj = (root.VeraISO && typeof root.VeraISO.proj === 'function') ? root.VeraISO.proj(30, 45, 1, true) : null;
        const den = (this.ownerDocument && this.ownerDocument.documentElement.getAttribute('data-den') || 'full').toLowerCase(); this.dataset.den = den;
        const o = layout(S.scene, S.mode, W, H, { layer: S.layer, proj: (root.VeraISO && typeof root.VeraISO.proj === 'function') ? root.VeraISO.proj(38, 45, 1, true) : null, den, stack: S.stack, wsz: S.wsz }); this._last = o;
        this.querySelectorAll('.xp-ctl button[data-a="stack"]').forEach((b) => { b.classList.toggle('on', !!S.stack && o.mode === 'iso'); b.style.display = o.mode === 'iso' ? '' : 'none'; });
        this.querySelectorAll('.xp-ctl button[data-a="wsz"]').forEach((b) => { b.textContent = (S.wsz || 'm').toUpperCase(); b.style.display = o.mode === 'iso' ? '' : 'none'; });
        if (this._r.scrub) { this._r.scrub.max = String(Math.max(0, (S.scene.turns || []).length - 1)); this._r.scrub.value = String(o.sel); }
        this.querySelectorAll('.xp-ctl button[data-m]').forEach((b) => b.classList.toggle('on', b.dataset.m === o.mode));
        this._r.dots.innerHTML = (S.scene.turns || []).map((t, i) => '<div class="xp-dot' + (i === o.sel ? ' on' : '') + '" data-mid="' + esc(t.mid) + '" title="' + esc((t.who || 'you') + ' · ' + (t.t || '') + ' · ' + String(t.text || '').slice(0, 80)) + '"><span class="l">' + esc((t.who || 'you') + ' ' + (i + 1) + ' · ' + String(t.text || '').slice(0, 30)) + '</span><i></i></div>').join('');
        view.classList.toggle('scroll', o.mode === 'cards');
        const st = (x, y) => 'left:' + x + 'px;top:' + y + 'px;';
        const itHtml = (c, anch) => '<div class="xp-it' + (anch ? ' anch' : '') + (c.card && c.card.src ? ' has-img' : '') + (S.open === c.id ? ' open' : '') + '" data-id="' + esc(c.id) + '" title="' + esc(c.card.n || '') + (c.card.d ? ' — ' + esc(c.card.d) : '') + '" style="' + st(c.x, c.y) + 'width:' + c.w + 'px;height:' + c.h + 'px;--cc:' + esc(c.col) + '"><span class="n">' + (c.card.tpl ? '<i class="tpl" title="placed from the registry · ' + esc(c.card.tpl) + '">⧉</i> ' : '') + esc(c.card.n || '') + '</span><span class="d">' + esc(c.card.d || '') + '</span><div class="b">' + cardBody(c.card) + '</div></div>';
        let h = '';
        if (!(S.scene.turns || []).length) { view.innerHTML = '<div class="xp-empty">Nothing to explode yet — the scene is the session\'s turns: what each read, what it said, what it made, where it landed.</div>'; view.style.transform = 'none'; this._emit(o); return; }
        if (o.mode === 'front') {
          const nav = '<button class="xp-nav l" data-a="prev">‹</button><button class="xp-nav r" data-a="next">›</button>';
          h = '<div class="xp-car"><div class="xp-track"><div class="xp-spine"></div>' + o.leaders.map((l) => '<div class="xp-lead" style="transform:' + l.tf + ';width:' + l.w + '"></div>').join('')
            + o.panels.map((p) => '<div class="xp-cp ' + p.cls + '" data-li="' + p.li + '" style="--pc:' + esc(p.col) + ';--d:' + p.d + ';transform:' + p.tf + '"><div class="xp-cp-h"><i></i>' + esc(p.name) + '<span style="letter-spacing:0;text-transform:none;opacity:.7"> · ' + esc(p.sub) + '</span><b>' + p.n + '</b></div><div class="xp-cp-b">'
              + (p.graph && p.graph.nodes.length ? '<div class="xp-gp">' + graphHtml(p.graph, 260) + '</div>' : '')
              + p.cards.map((c) => '<div class="xp-rc' + (S.open === c.id ? ' open' : '') + '" data-id="' + esc(c.id) + '" style="--cc:' + esc(c.col) + '"><span class="n">' + esc(c.card.n || '') + '</span><span class="d">' + esc(c.card.d || '') + '</span><div class="b">' + cardBody(c.card) + '</div></div>').join('')
              + (p.cards.length ? '' : '<div class="d" style="color:var(--xp-t3);font-family:var(--xp-mono);font-size:9px">nothing here for this turn</div>') + '</div></div>').join('') + '</div>' + nav + '</div>';
          view.innerHTML = h; view.style.transform = 'none'; this._emit(o); return;
        }
        const iwHtml = (wg) => { const open = S.open === wg.id; const fw = open ? Math.round(wg.w * 1.35) : wg.w, fh = open ? Math.round(wg.h * 1.35) : wg.h; const isImg = wg.form === 'image';
          return '<div class="xp-iw' + (open ? ' open' : '') + '" data-id="' + esc(wg.id) + '" title="' + esc(wg.card.n || '') + (wg.card.d ? ' — ' + esc(wg.card.d) : '') + '" style="' + st(wg.x, wg.y) + '--cc:' + esc(wg.col) + '">'
            + '<span class="xp-stem" style="height:' + wg.stem + 'px"></span>'
            + '<div class="xp-if' + (isImg ? ' img' : '') + (wg.sample ? ' sample' : '') + '" style="width:' + fw + 'px;height:' + fh + 'px;bottom:' + wg.stem + 'px"><div class="xp-if-hd" title="' + esc(wg.form) + '"><i></i><i></i><i></i><span>' + esc(wg.card.d || wg.form) + '</span>' + (wg.placed ? '<i class="tpl" title="placed from the registry · ' + esc(wg.card.tpl || '') + '">⧉</i>' : '') + '</div><div class="xp-if-bd">' + frameHtml(wg, fh - 21) + '</div></div>'
            + '<span class="xp-cap"><span class="n">' + esc(wg.card.n || '') + '</span><span class="d">' + esc(wg.card.d || '') + (wg.sample ? ' · no reading yet' : '') + '</span></span>'
            + '<div class="b">' + cardBody(wg.card) + '</div></div>'; };
        if (o.mode === 'iso') {
          this.classList.toggle('stacked', !!o.stack);
          o.plates.forEach((p) => { const xs = p.poly.map((q) => q.x), ys = p.poly.map((q) => q.y); const x0 = Math.min.apply(null, xs), y0 = Math.min.apply(null, ys), x1 = Math.max.apply(null, xs), y1 = Math.max.apply(null, ys); const cp = 'polygon(' + p.poly.map((q) => (q.x - x0).toFixed(1) + 'px ' + (q.y - y0).toFixed(1) + 'px').join(',') + ')'; h += '<div class="xp-pl iso' + (p.cls ? ' ' + p.cls : '') + '" style="' + st(x0, y0) + 'width:' + (x1 - x0).toFixed(1) + 'px;height:' + (y1 - y0).toFixed(1) + 'px;--cp:' + cp + '"></div>'; });
        } else { o.plates.forEach((p) => { h += '<div class="xp-pl' + (p.cls ? ' ' + p.cls : '') + '" style="' + st(p.x, p.y) + 'width:' + p.w + 'px;height:' + p.h + 'px"></div>'; }); }
        o.edges.forEach((e) => { h += '<div class="xp-e ' + e.cls + '" title="' + esc(e.title) + '" style="' + st(e.x, e.y) + 'width:' + e.len + 'px;--ec:' + esc(e.col) + ';transform:rotate(' + e.deg + 'deg)"></div>'; });
        o.labels.forEach((l) => { h += '<div class="xp-lb ' + l.cls + '" data-mid="' + esc(l.mid || '') + '" style="' + st(l.x, l.y) + 'color:' + esc(l.col) + '">' + esc(l.n) + '<b>' + esc(l.k) + '</b></div>'; });
        (o.bands || []).forEach((b) => { const xs = b.poly.map((q) => q.x), ys = b.poly.map((q) => q.y); const x0 = Math.min.apply(null, xs), y0 = Math.min.apply(null, ys), x1 = Math.max.apply(null, xs), y1 = Math.max.apply(null, ys); const cp = 'polygon(' + b.poly.map((q) => (q.x - x0).toFixed(1) + 'px ' + (q.y - y0).toFixed(1) + 'px').join(',') + ')'; h += '<div class="xp-band ' + esc(b.layer) + (b.empty ? ' empty' : '') + (b.cls ? ' ' + b.cls : '') + '" style="' + st(x0, y0) + 'width:' + (x1 - x0).toFixed(1) + 'px;height:' + (y1 - y0).toFixed(1) + 'px;--cp:' + cp + ';--bc:' + esc(b.col) + '"></div>'; });
        o.cards.forEach((c) => { if (!c.iw) h += itHtml(c, !!c.anchored); });
        (o.widgets || []).slice().sort((a, b) => a.y - b.y).forEach((wg) => { h += iwHtml(wg); });
        o.graphs.forEach((g) => { h += g.iso
          ? '<div class="xp-g iso" data-id="' + esc(g.id) + '" style="' + st(g.x, g.y) + 'width:' + g.w + 'px;height:' + g.h + 'px">' + (root.VeraWidget && typeof root.VeraWidget.draw === 'function' ? root.VeraWidget.draw('context_graph', { nodes: g.data.nodes, rels: g.data.rels }, 'm', { height: g.h, width: g.w, bare: true, view: 'iso', full: false, hubMeta: g.data.nodes.length + ' rec' }) : graphHtml(g.data, g.h)) + '</div>'
          : '<div class="xp-g" data-id="' + esc(g.id) + '" style="' + st(g.x, g.y) + 'width:' + g.w + 'px;height:' + g.h + 'px">' + graphHtml(g.data, g.h - 10) + '</div>'; });
        o.gnodes.forEach((n) => { h += '<div class="xp-gn' + (n.ghost ? ' ghost' : '') + '" data-id="' + esc(n.id) + '" title="' + esc(n.label + ' · ' + n.lane) + '" style="' + st(n.x, n.y) + '--cc:' + esc(n.col || 'var(--xp-dv1)') + '"></div>'; });
        if (o.mode === 'cards') h = '<div style="position:relative;width:' + o.size.w + 'px;height:' + o.size.h + 'px">' + h + '</div>';
        view.innerHTML = h; this._applyPan(); this._emit(o);
      }
      _emit(o) { this.dispatchEvent(new CustomEvent('vera:xpl:rendered', { detail: { mode: o.mode, stations: o.stations, cards: o.cards.length, panels: o.panels.length, graphs: (o.graphs || []).length, gnodes: (o.gnodes || []).length }, bubbles: true })); }
    }
    root.customElements.define('vera-exploded', VeraExploded);
  }
  const api = { layout, LAYERS, ensureCss, graphData, widgetOf, version: 3 };
  root.VeraExploded = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
