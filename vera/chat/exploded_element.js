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

   The iso's items are drawn the way the Canvas board draws its exploded iso (Canvas.dc.html, the .xit / .xig / .xnd
   vocabulary): every item is the board's CARD standing on a stem at its pin — name · value · meta · a small body by
   kind (a score bar, chart bars, a diff, table rows, the loop's steps, a line of code or terminal); a widget (placed
   from the registry, or one the reply carried) is an ISO WIDGET GROUP — a dial, bars or a block built through the
   ISO lib's box/face/scene — with a frameless caption beneath it (the card is only its label); the records in the
   context-graph band are typed ICON NODES lying on the plate about the prompt line, the galaxy sheet past them.
   The items counter-scale by the FIT only (1:1 text when the scene is fitted, capped as the board's embed is), so
   the wheel / ± zoom grows and shrinks them WITH the scene.

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
  // the board's typed icons: a graphed entity looks like what it is — the node carries its type rather than being one more dot
  const ICON = {
    person: 'M8 8.4a2.8 2.8 0 1 0 0-5.6 2.8 2.8 0 0 0 0 5.6Zm-4.9 5.2a4.9 4.9 0 0 1 9.8 0',
    host: 'M2.4 3.6h11.2v6.8H2.4zM5.6 13h4.8M8 10.4V13',
    ssh: 'M2.4 3.4h11.2v9.2H2.4zM4.9 6.6l2 1.8-2 1.8M8.6 10.4h3',
    container: 'M8 2.5 13.5 5.2v5.6L8 13.5 2.5 10.8V5.2zM2.5 5.2 8 7.9l5.5-2.7M8 7.9v5.6',
    service: 'M8 3.2a4.8 4.8 0 1 0 0 9.6 4.8 4.8 0 0 0 0-9.6Zm0 3.1a1.7 1.7 0 1 1 0 3.4 1.7 1.7 0 0 1 0-3.4Z',
    file: 'M4.2 2.4h4.9l3 3v8.2H4.2zM9.1 2.4v3h3',
    commit: 'M8 5.4a2.6 2.6 0 1 0 0 5.2 2.6 2.6 0 0 0 0-5.2ZM8 2.2v3.2M8 10.6v3.2',
    memory: 'M2.6 5 8 2.6 13.4 5 8 7.4zM2.6 8 8 10.4 13.4 8M2.6 11 8 13.4 13.4 11',
    skill: 'M8 2.5l1.7 3.5 3.8.5-2.8 2.7.7 3.8L8 11.2l-3.4 1.8.7-3.8L2.5 6.5l3.8-.5z',
    cap: 'M6.4 2.4v3.3M9.6 2.4v3.3M4.7 5.7h6.6v2.6a3.3 3.3 0 0 1-6.6 0zM8 11.6v2',
    step: 'M5.2 3.2 11 8l-5.8 4.8z',
    plan: 'M3.4 4.2h9.2M3.4 8h9.2M3.4 11.8h5.6',
    canvas: 'M2.6 3.2h10.8v9.6H2.6zM2.6 7.4h10.8M8 3.2v9.6',
    dataset: 'M8 2.6c3 0 5.2.8 5.2 1.8v7.2c0 1-2.2 1.8-5.2 1.8s-5.2-.8-5.2-1.8V4.4C2.8 3.4 5 2.6 8 2.6ZM2.8 4.4c0 1 2.2 1.8 5.2 1.8s5.2-.8 5.2-1.8',
    page: 'M3.6 2.6h8.8v10.8H3.6zM5.8 5.6h4.4M5.8 8h4.4M5.8 10.4h2.6',
    clip: 'M6.2 9.6l4.6-4.6a1.9 1.9 0 0 1 2.7 2.7l-5.6 5.6a3.2 3.2 0 0 1-4.5-4.5L9 3.2'
  };
  const ICON_OF = (kind, lane) => { const k = String(kind || '').toLowerCase();
    if (/chunk|code|file|doc/.test(k)) return ICON.file; if (/memory|recall/.test(k)) return ICON.memory; if (/dataset|record|table/.test(k)) return ICON.dataset;
    if (/commit|sha|git/.test(k)) return ICON.commit; if (/page|web|url/.test(k)) return ICON.page; if (/cap|tool/.test(k)) return ICON.cap; if (/step|loop|run/.test(k)) return ICON.step;
    if (/plan|goal/.test(k)) return ICON.plan; if (/skill/.test(k)) return ICON.skill; if (/attach/.test(k)) return ICON.clip; if (/person|user|you/.test(k)) return ICON.person;
    if (/host|node|machine/.test(k)) return ICON.host; if (/container|docker|sandbox/.test(k)) return ICON.container; if (/canvas|note|widget|pin/.test(k)) return ICON.canvas;
    if (lane === 'canvas') return ICON.canvas; if (lane === 'loop') return ICON.step; if (lane === 'plan') return ICON.plan; if (lane === 'memory') return ICON.memory; return ICON.service; };
  const LANES = ['context', 'memory', 'loop', 'plan', 'canvas'];

  /* ── the layout, pure ─────────────────────────────────────────────────────────────────────────────── */
  function layout(scene, mode, W, H, o) {
    o = o || {}; const turns = (scene && scene.turns) || []; mode = mode === 'front' || mode === 'iso' ? mode : 'cards';
    const selIdx = Math.max(0, turns.findIndex((t) => t.mid === (scene && scene.sel))); const sel = turns.length ? Math.max(0, selIdx) : 0;
    const out = { mode, stations: turns.length, sel, plates: [], labels: [], cards: [], edges: [], panels: [], leaders: [], graphs: [], gnodes: [], size: { w: W, h: H }, fit: { s: 1, x: 0, y: 0 }, inv: 1 };
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
    // floors, one above the other). Every band is drawn; every item is the board's card on a stem at its pin, a widget
    // an iso widget group with its caption, the context records icon nodes about the prompt line, the galaxy past them.
    const P = o.proj || isoP(30, 45);   // the classic isometric the whole design draws with
    const STK = !!o.stack, WSZ = o.wsz === 's' || o.wsz === 'l' ? o.wsz : 'm';
    const FW = { s: 60, m: 78, l: 108 }[WSZ], FH = Math.round(FW * 0.56);   // the iso widgets' footprint — S · M · L
    const CW = STK ? 172 : 200, CH = STK ? 47 : 54, RAISE = STK ? 10 : 14;   // the board's card (tighter on a stack), standing on its stem
    const RV = STK ? 200 : 300, CU = STK ? 340 : 420, ROWMAX = 3, CAP = 6;  // the lattice (the board's pitches: a row clears a card, a column clears its width); six per band, the rest a count
    const HEAD = STK ? 100 : 130, FOOT = 30;                                  // headroom above a band's first row (the cards stand up from their pins), the room past its last
    const LV = 64, GAL = { w: 200, h: 84 }, ICAP = 1.45;                      // a lane row's pitch in the graph band; the galaxy sheet lying past the nodes; the counter-scale's cap
    const proj = (u, v, z) => { const p = P(u, v, z || 0); return { x: p[0], y: p[1] }; };
    const yPerV = Math.max(0.05, Math.abs(proj(0, 100, 0).y - proj(0, 0, 0).y) / 100);   // screen px down per v unit, through P
    out.bands = []; out.widgets = []; out.stack = STK; out.wsz = WSZ;
    const isWidget = (c) => !!(c && (c.tpl || c.form || String(c.kind || '').toLowerCase() === 'widget'));
    const shownOf = (t, L) => cardsOf(t, L.key).slice(0, CAP);
    const colsOf = (n) => Math.max(1, Math.ceil(n / ROWMAX)), rowsOf = (n) => Math.max(1, Math.min(ROWMAX, n));
    // the bands are as deep as their fullest station needs, so the plates stay identical and the bands line up across them
    const graphs = turns.map(graphData);
    const laneDepth = (g) => LANES.filter((l) => g.laneList.indexOf(l) >= 0).reduce((s, l) => s + LV * Math.ceil(g.nodes.filter((n) => n.lane === l).length / 2), 0);
    const anyGraph = graphs.some((g) => g.nodes.length);
    const GNODES = 40 + Math.max.apply(null, graphs.map(laneDepth).concat([LV])) + 20, GALV = anyGraph ? Math.round(GAL.h * ICAP / yPerV) + 40 : 0;
    const VBS = LAYERS.map((L) => L.kind === 'graph' ? GNODES + GALV + 30 : HEAD + RV * (Math.max.apply(null, turns.map((t) => rowsOf(shownOf(t, L).length))) - 1) + FOOT);
    const V0S = VBS.map((_, i) => VBS.slice(0, i).reduce((a, b) => a + b, 0)); const PH = VBS.reduce((a, b) => a + b, 0);
    const COLS = Math.max.apply(null, turns.map((t) => Math.max.apply(null, LAYERS.filter((L) => L.kind !== 'graph').map((L) => colsOf(shownOf(t, L).length)))));
    const PW = 360 + (COLS - 1) * CU; const pts = [];
    // a floor is held clear of the one beneath: the plate's projected depth plus a gap, in z (measured through P)
    const ZH = (() => { const a = proj(0, 0, 0), b = proj(PW, PH, 0), c = proj(0, 0, 100); const perZ = Math.max(0.05, (a.y - c.y) / 100); return Math.round((Math.abs(b.y - a.y) + 90) / perZ); })();
    let uRun = 0; const nodePins = {};
    turns.forEach((t, si) => {
      const u0 = STK ? 0 : uRun, z = STK ? (turns.length - 1 - si) * ZH : 0; if (!STK) uRun += PW + 160;
      const corners = [proj(u0, 0, z), proj(u0 + PW, 0, z), proj(u0 + PW, PH, z), proj(u0, PH, z)]; corners.forEach((c) => pts.push(c));
      out.plates.push({ si, mid: t.mid, cls: si === sel ? 'on' : '', poly: corners.map((c) => ({ x: c.x, y: c.y })), z });
      const lb = proj(u0, -18, z); out.labels.push({ si, x: lb.x, y: lb.y, n: (t.who || 'you') + ' · ' + (t.t || ''), k: String(t.text || '').slice(0, 40), cls: 'station' + (si === sel ? ' on' : ''), col: 'var(--xp-t2)', mid: t.mid });
      LAYERS.forEach((L, li) => {
        const v0 = V0S[li], VB = VBS[li]; const list = cardsOf(t, L.key);
        // the band on the plate — drawn whether or not the turn has anything in it
        const bc = [proj(u0 + 4, v0 + 4, z), proj(u0 + PW - 4, v0 + 4, z), proj(u0 + PW - 4, v0 + VB - 4, z), proj(u0 + 4, v0 + VB - 4, z)];
        const ll = proj(u0 + PW + 8, v0 + 10, z);
        if (L.kind === 'graph') { const g = graphs[si];
          out.labels.push({ si, x: ll.x, y: ll.y, n: L.name, k: String(g.nodes.length), cls: 'layer sm ' + L.key, col: L.col });
          out.bands.push({ si, mid: t.mid, layer: L.key, poly: bc.map((c) => ({ x: c.x, y: c.y })), col: L.col, empty: !g.nodes.length, cls: si === sel ? 'on' : '' });
          if (!g.nodes.length) { const e = proj(u0 + PW / 2, v0 + VB / 2, z); out.labels.push({ si, x: e.x, y: e.y, n: 'nothing read', k: '', cls: 'layer sm empty', col: 'var(--xp-t3)' }); return; }
          // the prompt line runs down the band's middle; a record sits nearer it the more relevant it is, a lane per family down the band
          const um = u0 + PW / 2, SPR = PW / 2 - 60; let lv = v0 + 40;
          const a0 = proj(um, v0 + 16, z), a1 = proj(um, v0 + GNODES - 16, z); out.edges.push({ x: px(a0.x), y: px(a0.y), len: px(Math.hypot(a1.x - a0.x, a1.y - a0.y)), deg: +(Math.atan2(a1.y - a0.y, a1.x - a0.x) * 180 / Math.PI).toFixed(2), col: 'var(--xp-dv1)', cls: 'thin prompt', title: 'the prompt — nearer the line, more relevant', raw: true });
          out.labels.push({ si, x: a0.x, y: a0.y - 14, n: 'the prompt', k: g.nodes.length + ' rec', cls: 'layer sm prompt', col: 'var(--xp-dv1)' });
          LANES.forEach((lane) => { const members = g.nodes.filter((n) => n.lane === lane).sort((a, b) => b.score - a.score); if (!members.length) return;
            const lp = proj(u0 - 8, lv + LV / 2, z); out.labels.push({ si, x: lp.x, y: lp.y, n: lane, k: String(members.length), cls: 'layer sm lane', col: members[0].col || L.col });
            members.forEach((n, mi) => { const row = Math.floor(mi / 2), side = mi % 2 ? 1 : -1; const rel = Math.max(0, Math.min(1, n.score));
              const p = proj(um + side * (26 + (1 - rel) * SPR), lv + row * LV + LV / 2, z); pts.push(p); nodePins[t.mid + '|' + n.id] = p;
              out.gnodes.push({ id: t.mid + ':graph:' + n.id, nid: n.id, mid: t.mid, si, x: p.x, y: p.y, d: Math.round(19 + rel * 9), col: n.col || L.col, icon: ICON_OF(n.kind, n.lane), label: n.label, lane: n.lane, kind: n.kind, score: rel, ghost: n.included === false, lit: rel > 0.82, op: +(0.45 + rel * 0.55).toFixed(2) }); });
            lv += LV * Math.ceil(members.length / 2); });
          // the context galaxy, iso view, lying in the band past the nodes: the same widget the Context menu draws
          const c = proj(u0 + PW / 2, v0 + GNODES + GALV / 2, z); pts.push(c); out.graphs.push({ id: t.mid + ':graph', mid: t.mid, si, x: c.x, y: c.y, w: GAL.w, h: GAL.h, data: g, iso: true });
          return; }
        out.labels.push({ si, x: ll.x, y: ll.y, n: L.name, k: String(list.length), cls: 'layer sm ' + L.key, col: L.col });
        out.bands.push({ si, mid: t.mid, layer: L.key, poly: bc.map((c) => ({ x: c.x, y: c.y })), col: L.col, empty: !list.length, cls: si === sel ? 'on' : '' });
        if (!list.length) { const e = proj(u0 + PW / 2, v0 + VB / 2, z); out.labels.push({ si, x: e.x, y: e.y, n: L.key === 'say' ? 'no reply yet' : L.key === 'read' ? 'nothing read' : L.key === 'made' ? 'nothing produced' : 'nothing landed', k: '', cls: 'layer sm empty', col: 'var(--xp-t3)' }); return; }
        // every item on its pin: the band fills down its rows first, then a column to the right (the board: the plate grows along u)
        const shown = list.slice(0, CAP), more = list.length - shown.length; const cols = colsOf(shown.length);
        if (more > 0) { const mp = proj(u0 + PW - 30, v0 + VB - 26, z); out.labels.push({ si, x: mp.x, y: mp.y, n: '+' + more + ' more', k: 'in the graph band', cls: 'layer sm more', col: L.col, mid: t.mid }); }
        shown.forEach((c, ci) => { const col = Math.floor(ci / ROWMAX), row = ci % ROWMAX; const p = proj(u0 + PW / 2 - (cols - 1) * CU / 2 + col * CU, v0 + HEAD + row * RV, z); pts.push(p);
          const wd = widgetOf(c); const id = t.mid + ':' + L.key + ':' + ci; const grp = isWidget(c);
          out.widgets.push({ id, mid: t.mid, si, layer: L.key, ci, x: p.x, y: p.y, w: FW, h: FH, cw: CW, ch: CH, stem: RAISE, card: c, col: c.col || L.col, form: wd.form, data: wd.data, placed: !!c.tpl, sample: !!wd.sample, draw: grp ? 'group' : 'card', value: grp ? valueOf(wd) : '', tight: STK });
          out.cards.push({ id, mid: t.mid, si, layer: L.key, ci, x: p.x, y: p.y, w: CW, h: CH, card: c, col: c.col || L.col, anchored: true, iw: true }); });
      });
    });
    // fit the scene into the frame: scale and shift. The items counter-scale by the FIT alone (1:1 text when fitted,
    // capped as the board's embed caps it) — the pan zoom then grows and shrinks them with the scene
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity; pts.forEach((p) => { x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y); x1 = Math.max(x1, p.x); y1 = Math.max(y1, p.y); });
    const s = Math.max(0.3, Math.min(1.4, Math.min((W - 80) / Math.max(1, x1 - x0 + 200), (H - 120) / Math.max(1, y1 - y0 + 140))));
    const dx = W / 2 - s * (x0 + x1) / 2, dy = H / 2 - s * (y0 + y1) / 2 + 20;
    out.fit = { s: +s.toFixed(3), x: px(dx), y: px(dy) };
    // the fit is baked into the coordinates (the board scales its stage instead), so the items' DOM scale is the board's
    // rule applied at the fit: 1:1 when fitted, grown no more than 1.45× against a small scene — s · min(1.45, 1/s)
    out.inv = +(s * Math.min(1.45, 1 / Math.min(1, s))).toFixed(3);
    const T = (p) => ({ x: px(p.x * s + dx), y: px(p.y * s + dy) });
    out.plates.forEach((pl) => { pl.poly = pl.poly.map(T); });
    out.bands.forEach((b) => { b.poly = b.poly.map(T); });
    out.labels.forEach((l) => { const q = T(l); l.x = q.x; l.y = q.y; });
    out.cards.forEach((c) => { const q = T(c); c.x = q.x; c.y = q.y; });
    out.widgets.forEach((c) => { const q = T(c); c.x = q.x; c.y = q.y; });
    out.gnodes.forEach((n) => { const q = T(n); n.x = q.x; n.y = q.y; });
    out.graphs.forEach((g) => { const q = T(g); g.x = q.x; g.y = q.y; });
    Object.keys(nodePins).forEach((k) => { nodePins[k] = T(nodePins[k]); });
    const kept = out.edges.filter((e) => e.raw).map((e) => { const a = T({ x: e.x, y: e.y }); return Object.assign({}, e, { x: a.x, y: a.y, len: px(e.len * s), raw: undefined }); });
    out.edges = kept; // re-derive at screen scale: the runs between the pins, the relations between the nodes
    turns.forEach((t) => { const byL = {}; out.widgets.filter((c) => c.mid === t.mid).forEach((c) => { (byL[c.layer] = byL[c.layer] || []).push(c); });
      const gr = out.graphs.find((g) => g.mid === t.mid);
      const ex = (byL.say || [])[0]; if (ex) { (byL.read || []).forEach((r) => edge(r, ex, 'var(--xp-dv1)', 'in', 'read by this turn')); if (gr && !(byL.read || []).length) edge(gr, ex, 'var(--xp-dv1)', 'in', 'the context this turn read'); (byL.made || []).forEach((r) => edge(ex, r, 'var(--xp-dv2)', 'out', 'produced by this turn')); }
      (byL.made || []).forEach((r, i) => { const l = (byL.land || [])[i] || (byL.land || [])[0]; if (l) edge(r, l, 'var(--xp-ac)', 'link', 'this became a canvas item'); });
      graphData(t).rels.forEach((r) => { const a = nodePins[t.mid + '|' + r.from], b = nodePins[t.mid + '|' + r.to]; if (a && b) edge(a, b, r.kind === 'mem' ? 'var(--xp-ac2)' : 'var(--xp-dv1)', 'rel', r.kind === 'mem' ? 'a memory that relates' : 'cites'); }); });
    out.size = { w: W, h: H };
    return out;
  }
  /* ── an item as a WIDGET: the form and the data a widget of this kind reads ─────────────────────────────── */
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
  // the numbers a widget's data holds, for its bars and its sparkline
  function seriesOf(wd) { const d = wd.data, s = SHAPE[wd.form] || 'string';
    if (s === 'series' && Array.isArray(d)) return d.map(Number).filter((v) => isFinite(v)).slice(-8);
    if (s === 'values' && d && typeof d === 'object' && !Array.isArray(d)) return Object.keys(d).map((k) => +d[k]).filter((v) => isFinite(v)).slice(0, 8);
    if (s === 'level' && d && d.value != null) return [Math.max(0, Math.min(1, +d.value / (+d.max || 100)))];
    return []; }
  // the reading the caption carries beside the name (the board's xit-cv): the level, the last of a series, the top of a set
  function valueOf(wd) { const d = wd.data, s = SHAPE[wd.form] || 'string'; if (wd.sample) return '';
    if (s === 'level' && d && d.value != null) return d.max ? Math.round(+d.value / +d.max * 100) + '%' : String(d.value);
    if (s === 'series' && Array.isArray(d) && d.length) return String(d[d.length - 1]);
    if (s === 'values' && d && typeof d === 'object' && !Array.isArray(d)) { const ks = Object.keys(d).filter((k) => isFinite(+d[k])); if (!ks.length) return ''; const top = ks.reduce((a, b) => (+d[b] > +d[a] ? b : a)); return top + ' ' + d[top]; }
    if (s === 'items' && Array.isArray(d)) return d.length + ' rows'; if (s === 'events' && Array.isArray(d)) return d.length + ' lines'; if (s === 'stages' && d && Array.isArray(d.steps)) return d.steps.length + ' steps';
    return ''; }
  /* ── an ISO WIDGET GROUP: the widget as an object on the plate — a dial for a level, bars for a set or a series, a
     block for anything else — built through the ISO lib's box/face/scene about (0,0), the item's pin. Pure: the lib
     is handed in (window.VeraISO in the page), so a node test can build one too. Returns null without the lib. ─── */
  function groupOf(wg, ISO, o) {
    if (!ISO || typeof ISO.scene !== 'function' || typeof ISO.proj !== 'function') return null;
    o = o || {}; const FW = wg.w || 78, col = wg.col || 'var(--xp-ac)', shape = SHAPE[wg.form] || 'string'; const wd = { form: wg.form, data: wg.data, sample: wg.sample };
    const vals = seriesOf(wd); let boxes = [], k = FW / 4, key = null, needle = null, big = null, kind = 'block';
    if (shape === 'level') { kind = 'dial'; const val = vals.length ? vals[0] : 0; const N = 18, R = 2.2, lit = Math.round(N * val); k = FW / 5.6;
      for (let i = 0; i < N; i++) { const a = Math.PI * .75 + (i / (N - 1)) * Math.PI * 1.5; const on = i < lit; boxes.push({ u: Math.cos(a) * R - .18, v: Math.sin(a) * R - .18, z: 0, w: .36, d: .36, h: on ? .32 : .1, col: !on ? 'var(--xp-s3)' : (i < N * .6 ? 'var(--xp-ac2)' : i < N * .85 ? 'var(--xp-dv2)' : 'var(--xp-red)') }); }
      boxes.push({ u: -.4, v: -.4, z: 0, w: .8, d: .8, h: .42, col: 'var(--xp-s3)' }); key = (b) => (b.u + b.v) * 100 + b.z;
      needle = { an: Math.PI * .75 + (Math.max(1, lit) - 1) / (N - 1) * Math.PI * 1.5, R }; big = wg.value || (wg.sample ? '' : Math.round(val * 100) + '%'); }
    else if ((shape === 'values' || shape === 'series') && vals.length) { kind = 'bars'; const n = vals.length, mx = Math.max.apply(null, vals.map((v) => Math.abs(v))) || 1; k = FW / (n + .6);
      boxes.push({ u: -.3, v: -.3, z: -.16, w: n + .3, d: 1.3, h: .16, col: 'var(--xp-s3)' });
      vals.forEach((v, i) => boxes.push({ u: i + .15, v: 0, z: 0, w: .7, d: .7, h: .2 + Math.abs(v) / mx * 2.8, col })); key = (b) => b.z * 10 + b.u + b.v; }
    else { boxes.push({ u: 0, v: 0, z: 0, w: 3.2, d: 3.2, h: .4, col: 'var(--xp-s3)' }, { u: .5, v: .5, z: .4, w: 2.2, d: 2.2, h: 1.1, col }); key = (b) => b.z * 10 + b.u + b.v; }
    const Pd = ISO.proj(o.tilt == null ? 30 : o.tilt, o.azim == null ? 45 : o.azim, k); const f = ISO.scene(Pd, boxes, key);
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity; f.forEach((q) => { x0 = Math.min(x0, q.x); y0 = Math.min(y0, q.y); x1 = Math.max(x1, q.x + q.w); y1 = Math.max(y1, q.y + q.h); });
    const dx = -(x0 + x1) / 2, dy = -(y0 + y1) / 2; f.forEach((q) => { q.x += dx; q.y += dy; });
    const faces = (typeof ISO.px === 'function' ? ISO.px(f) : f.map((q, i) => Object.assign({}, q, { i, x: q.x.toFixed(1) + 'px', y: q.y.toFixed(1) + 'px', w: q.w.toFixed(1) + 'px', h: q.h.toFixed(1) + 'px' })));
    let nd = null; if (needle) { const a = Pd(0, 0, .46), b = Pd(Math.cos(needle.an) * needle.R * .8, Math.sin(needle.an) * needle.R * .8, .34); const ex = b[0] - a[0], ey = b[1] - a[1]; nd = { x: (a[0] + dx).toFixed(1) + 'px', y: (a[1] + dy).toFixed(1) + 'px', len: Math.hypot(ex, ey).toFixed(1) + 'px', deg: (Math.atan2(ey, ex) * 180 / Math.PI).toFixed(1) + 'deg' }; }
    const bw = x1 - x0, bh = y1 - y0;
    return { kind, faces, needle: nd, big: big ? { x: '0px', y: (-bh / 2 - 3).toFixed(1) + 'px', n: big } : null, bw, bh, sh: bh / 2 + 3 };
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
vera-exploded{display:flex;flex-direction:column;min-height:0;min-width:0;position:relative;overflow:hidden;--xp-bg:var(--bg0,#0e0f12);--xp-s1:var(--bg1,#15171c);--xp-s2:var(--bg2,#1b1e25);--xp-s3:var(--bg3,#232732);--xp-bd:var(--border,#2a2e37);--xp-bd2:var(--border2,#3a3f4b);--xp-t1:var(--fg,#e6e6e6);--xp-t2:var(--dim,#aaa);--xp-t3:var(--dim2,#777);--xp-ac:var(--acc,#7c9cff);--xp-ac2:var(--acc2,#5ec9a0);--xp-dv1:#a78bfa;--xp-dv2:#fb923c;--xp-dv3:#38bdf8;--xp-red:#e06c75;--xp-mono:var(--mono,ui-monospace,monospace);font-size:10.5px;color:var(--xp-t1);background:var(--xp-bg)}
vera-exploded .xp-ctl{position:absolute;left:14px;top:10px;z-index:30;display:flex;align-items:center;gap:5px;padding:5px 8px;border-radius:8px;background:color-mix(in srgb,var(--xp-s1) 90%,transparent);box-shadow:0 0 0 1px var(--xp-bd)}
vera-exploded .xp-ctl .c{font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--xp-t3);margin-right:3px}
vera-exploded .xp-ctl button{font:inherit;font-size:10px;color:var(--xp-t2);background:none;border:0;cursor:pointer;padding:3px 10px;border-radius:999px}
vera-exploded .xp-ctl button.on{background:var(--xp-ac);color:var(--xp-bg)}
vera-exploded .xp-ctl .sep{width:1px;height:14px;background:var(--xp-bd);margin:0 3px}
vera-exploded .xp-it .tpl,vera-exploded .xit .tpl{font-style:normal;color:var(--xp-ac);font-size:10px}
vera-exploded .xp-scrub{width:96px;accent-color:var(--xp-ac);margin:0 2px 0 6px;cursor:pointer}
vera-exploded .xp-g{position:absolute;z-index:8;border-radius:6px;background:var(--xp-s2);box-shadow:0 0 0 1px var(--xp-bd);padding:4px 6px;box-sizing:border-box;overflow:hidden}
vera-exploded .xp-gf i{position:absolute;width:9px;height:9px;border-radius:50%;transform:translate(-50%,-50%);box-shadow:inset 0 0 0 1.5px var(--cc);background:color-mix(in srgb,var(--cc) 32%,transparent)}
vera-exploded .xp-img{display:block;max-width:100%;max-height:56px;border-radius:4px;margin-top:3px;object-fit:cover}
vera-exploded[data-den="hover"] .xp-img,vera-exploded[data-den="zen"] .xp-img{display:none}
vera-exploded[data-den="hover"] .xp-it:hover .xp-img,vera-exploded[data-den="hover"] .xp-rc:hover .xp-img,vera-exploded[data-den="zen"] .xp-it.open .xp-img,vera-exploded[data-den="zen"] .xp-rc.open .xp-img,vera-exploded[data-den="hover"] .xit:hover .xp-img,vera-exploded[data-den="zen"] .xit.open .xp-img{display:block;position:absolute;left:0;top:100%;z-index:40;max-height:220px;max-width:280px;box-shadow:0 6px 18px rgba(0,0,0,.5)}
vera-exploded[data-den="hover"] .xp-it.has-img,vera-exploded[data-den="zen"] .xp-it.has-img{overflow:visible}
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
vera-exploded .xp-e.thin{height:1px;opacity:.5;box-shadow:none}vera-exploded .xp-e.rel{height:1px;opacity:.55;box-shadow:none;z-index:7}
vera-exploded .xp-it{position:absolute;border-radius:6px;background:var(--xp-s2);box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-bd);cursor:pointer;z-index:10;padding:6px 10px;display:flex;flex-direction:column;gap:2px;box-sizing:border-box;overflow:hidden;transition:box-shadow .15s ease}
vera-exploded .xp-it:hover{box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-t3),0 14px 28px -18px rgba(0,0,0,.95);z-index:22}
vera-exploded .xp-it.open{height:auto!important;z-index:26;background:color-mix(in srgb,var(--xp-s1) 97%,transparent);box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1.5px var(--xp-ac),0 26px 46px -18px rgba(0,0,0,.98)}
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
/* ISO ITEMS — the board's card (Canvas.dc.html .xit): a billboard standing on its stem, name · meta · a body by kind. It
   counter-scales by the fit alone (--inv, set once per render), about its foot, so the zoom grows it with the scene */
vera-exploded .xstem{position:absolute;width:1px;transform:translateX(-.5px);background:linear-gradient(180deg,color-mix(in srgb,var(--sc) 80%,transparent),color-mix(in srgb,var(--sc) 30%,transparent));pointer-events:none;z-index:2}
vera-exploded .xstem i{position:absolute;left:-3px;bottom:-3px;width:6px;height:6px;border-radius:50%;background:var(--sc);box-shadow:0 0 0 2px var(--xp-s1)}
vera-exploded .xit{position:absolute;height:auto;min-height:var(--ih,54px);border-radius:6px;background:var(--xp-s2);transform:translateY(-100%) scale(var(--inv,1));transform-origin:50% 100%;box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-bd);cursor:pointer;z-index:10;padding:7px 11px;display:flex;flex-direction:column;gap:2px;box-sizing:border-box;transition:box-shadow .15s ease;text-align:left}
vera-exploded .xit:hover{box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-bd2),0 14px 28px -18px rgba(0,0,0,.95);z-index:22}
vera-exploded .xit.open{z-index:26;background:color-mix(in srgb,var(--xp-s1) 97%,transparent);box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1.5px var(--xp-ac),0 2px 0 3px var(--xp-bg),0 26px 46px -18px rgba(0,0,0,.98)}
vera-exploded .xit-n{font-size:12px;color:var(--xp-t1);line-height:1.35;overflow:hidden;text-overflow:ellipsis;white-space:normal;flex-shrink:0;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}vera-exploded .xit.open .xit-n{-webkit-line-clamp:unset;display:block}
vera-exploded .xit.tight{height:var(--ih);min-height:0;overflow:hidden}vera-exploded .xit.tight .xit-n{white-space:nowrap;display:block}vera-exploded .xit.tight:hover .xit-n,vera-exploded .xit.tight.open .xit-n{white-space:normal}vera-exploded .xit.tight.open{height:auto;overflow:visible}
vera-exploded .xit.tight .xit-body{display:none}vera-exploded .xit.tight.open .xit-body,vera-exploded .xit.tight:hover .xit-body{display:flex}
vera-exploded .xit-d{font-family:var(--xp-mono);font-size:10px;color:var(--xp-t3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex-shrink:0}
vera-exploded .xit-cv{display:none}
vera-exploded .xit-body{display:flex;flex-direction:column;gap:3px;margin-top:4px}
vera-exploded .xit-x{display:none;flex-direction:column;gap:2px;margin-top:5px;padding-top:5px;box-shadow:inset 0 1px 0 0 var(--xp-bd);max-height:170px;overflow:auto}vera-exploded .xit.open .xit-x{display:flex}
vera-exploded .xit-x pre{margin:0;font-family:var(--xp-mono);font-size:9px;white-space:pre-wrap;color:var(--xp-t2);line-height:1.45}
vera-exploded .xit-m{display:flex;flex-wrap:wrap;gap:4px;margin-top:2px}vera-exploded .xit-b{font-family:var(--xp-mono);font-size:9px;color:var(--xp-ac);background:color-mix(in srgb,var(--xp-ac) 15%,transparent);border-radius:999px;padding:1px 7px}
/* the canvas item vocabulary, the board's: a card IS a chart, a table, a diff, a loop, a line of code — not a title with a caption under it */
vera-exploded .xf-score{display:flex;align-items:center;gap:6px}vera-exploded .xf-bar{flex:1;height:4px;border-radius:2px;background:var(--xp-s3);overflow:hidden}vera-exploded .xf-bar i{display:block;height:100%;border-radius:2px;background:var(--cc)}vera-exploded .xf-score b{font-family:var(--xp-mono);font-size:9px;font-weight:400;color:var(--xp-t3)}
vera-exploded .xf-chart{display:flex;align-items:flex-end;gap:3px;height:26px}vera-exploded .xf-chart i{flex:1;border-radius:2px 2px 0 0;background:var(--cc);opacity:.8;min-width:6px}
vera-exploded .xf-diff{display:flex;gap:6px;font-family:var(--xp-mono);font-size:10px}vera-exploded .xf-diff .p{color:var(--xp-dv2);font-weight:400}vera-exploded .xf-diff .m{color:var(--xp-red);font-weight:400}
vera-exploded .xf-tab{display:flex;flex-direction:column;gap:1px}vera-exploded .xf-tr{display:flex;gap:8px;font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3);padding:1px 0}vera-exploded .xf-tr b{margin-left:auto;color:var(--xp-t2);font-weight:400}
vera-exploded .xf-code,vera-exploded .xf-term{font-family:var(--xp-mono);font-size:9px;padding:3px 6px;line-height:1.55;border-radius:4px;background:var(--xp-s3);color:var(--xp-t2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}vera-exploded .xf-term{color:var(--xp-dv2)}
vera-exploded .xf-w{display:flex;align-items:flex-end;gap:8px}vera-exploded .xf-w b{font-size:20px;line-height:1;color:var(--xp-t1);font-weight:500;font-family:var(--xp-mono)}vera-exploded .xf-wd{font-size:9px;color:var(--xp-dv2);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
vera-exploded .xf-ws{display:flex;align-items:flex-end;gap:2px;height:20px}vera-exploded .xf-ws i{width:4px;border-radius:1px;background:var(--cc);opacity:.7}
vera-exploded .xf-loop{display:flex;flex-direction:column;gap:0;margin-top:2px}vera-exploded .xf-ls{display:flex;align-items:center;gap:7px;font-size:9.5px;color:var(--xp-t3);padding:2px 0;box-shadow:inset 0 -1px 0 0 var(--xp-bd)}vera-exploded .xf-ls:last-child{box-shadow:none}
vera-exploded .xf-ls i{width:6px;height:6px;border-radius:50%;flex-shrink:0;background:var(--xp-t3);opacity:.45}vera-exploded .xf-ls.done i,vera-exploded .xf-ls.ok i{background:var(--xp-dv2);opacity:1}vera-exploded .xf-ls.run i{background:var(--xp-ac);opacity:1;box-shadow:0 0 0 3px color-mix(in srgb,var(--xp-ac) 24%,transparent)}vera-exploded .xf-ls.fail i{background:var(--xp-red);opacity:1}
vera-exploded .xf-ls b{font-family:var(--xp-mono);font-weight:400;color:var(--xp-t2);width:44px;flex-shrink:0}vera-exploded .xf-ls.run b{color:var(--xp-t1)}vera-exploded .xf-ls span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
vera-exploded .xf-note{font-size:10.5px;line-height:1.5;color:var(--xp-t2);padding:5px 8px;border-radius:4px;background:var(--xp-s2);box-shadow:inset 2px 0 0 0 color-mix(in srgb,var(--cc) 60%,transparent)}
/* an ISO WIDGET GROUP (the board's .xig): 0×0 at the item's pin; the faces, needle and big value sit about that origin.
   It counter-scales like the cards and is lifted (--wsh) so its lowest face clears the caption beneath */
vera-exploded .xig{position:absolute;width:0;height:0;z-index:9;transform-origin:0 0;transform:scale(var(--inv,1)) translateY(var(--wsh,0px));cursor:pointer}
vera-exploded .xig.sample{opacity:.55}
vera-exploded .xiw{position:absolute;display:block;z-index:8;clip-path:var(--cp);background:var(--fc)}vera-exploded .xiw.t{box-shadow:inset 0 0 0 1px rgba(255,255,255,.1)}
vera-exploded .xiw-n{position:absolute;height:3px;transform-origin:0 50%;background:var(--xp-t1);border-radius:2px;z-index:9;box-shadow:0 0 8px 1px color-mix(in srgb,var(--xp-dv2) 60%,transparent)}
vera-exploded .xiw-b{position:absolute;transform:translate(-50%,-100%);font-family:var(--xp-mono);font-size:16px;font-weight:700;color:var(--xp-t1);z-index:9;text-shadow:0 1px 4px var(--xp-bg);white-space:nowrap}
/* the caption under a widget: ONE compact line — the name and, for a reading widget, the value beside it. It hangs
   from its top edge (the pin + 6px); the card is only its label (the board's .xit.frameless) */
vera-exploded .xit.frameless{background:transparent!important;box-shadow:none!important;min-height:0!important;height:auto!important;overflow:visible;flex-direction:row;justify-content:center;align-items:baseline;gap:7px;padding:0 4px;line-height:1.2;white-space:nowrap;transform:scale(var(--inv,1));transform-origin:50% 0}
vera-exploded .xit.frameless .xit-n{text-align:center;font-size:10px;line-height:1.2;color:var(--xp-t3);display:block;padding:0;overflow:visible;white-space:nowrap!important;text-shadow:0 1px 3px rgba(0,0,0,.9)}
vera-exploded .xit.frameless .xit-cv{display:inline;font-family:var(--xp-mono);font-size:10px;font-weight:700;line-height:1.2;color:var(--xp-t1);text-shadow:0 1px 3px rgba(0,0,0,.9)}
vera-exploded .xit.frameless .xit-d,vera-exploded .xit.frameless .xit-body,vera-exploded .xit.frameless .xit-m{display:none!important}
vera-exploded .xit.frameless .xit-x{position:absolute;left:50%;top:100%;transform:translateX(-50%);width:230px;margin-top:6px;padding:8px 10px;border-radius:6px;background:color-mix(in srgb,var(--xp-s1) 97%,transparent);box-shadow:0 0 0 1px var(--xp-ac),0 20px 40px -14px #000;white-space:normal;text-align:left;font-size:10px;color:var(--xp-t2);line-height:1.4;z-index:30}
vera-exploded .xit.frameless.open{z-index:27}
/* a CONTEXT NODE on the plate (the board's .xnd): a typed icon in a ring, lit when it is in the prompt, hollow when it only relates */
vera-exploded .xnd{position:absolute;border-radius:50%;z-index:8;color:var(--nc);display:flex;align-items:center;justify-content:center;background:color-mix(in srgb,var(--nc) 20%,var(--xp-s1));box-shadow:inset 0 0 0 1.4px var(--nc);transform:translate(-50%,-50%) scale(var(--inv,1));transform-origin:50% 50%;pointer-events:auto;cursor:pointer;box-sizing:border-box}
vera-exploded .xnd svg{width:62%;height:62%;display:block}
vera-exploded .xnd.lit{box-shadow:inset 0 0 0 1.4px var(--nc),0 0 0 3px color-mix(in srgb,var(--nc) 20%,transparent)}
vera-exploded .xnd.ghost{background:none;opacity:.55!important;box-shadow:inset 0 0 0 1.2px color-mix(in srgb,var(--nc) 45%,transparent)}
vera-exploded .xnd:hover{box-shadow:inset 0 0 0 1.4px var(--nc),0 0 0 3px color-mix(in srgb,var(--nc) 34%,transparent);opacity:1!important;z-index:23}
vera-exploded .xnd.sel{box-shadow:inset 0 0 0 1.4px var(--nc),0 0 0 2px var(--xp-bg),0 0 0 4.5px var(--nc);opacity:1!important;z-index:24}
vera-exploded .xnd .xnl{display:none;position:absolute;left:50%;top:100%;transform:translateX(-50%);margin-top:5px;font-size:10px;color:var(--xp-t1);white-space:nowrap;text-shadow:0 1px 3px var(--xp-bg);pointer-events:none}vera-exploded .xnd .xnl b{font-family:var(--xp-mono);font-size:9px;font-weight:400;color:var(--xp-t3);margin-left:5px}vera-exploded .xnd.sel .xnl,vera-exploded .xnd:hover .xnl{display:block}
/* the bands: every layer drawn on the plate, a faint strip; the empty ones dashed */
vera-exploded .xp-band{position:absolute;pointer-events:none;clip-path:var(--cp);background:color-mix(in srgb,var(--bc) 7%,transparent)}
vera-exploded .xp-band.empty{background:repeating-linear-gradient(135deg,color-mix(in srgb,var(--bc) 6%,transparent) 0 6px,transparent 6px 14px)}
vera-exploded .xp-band.on{background:color-mix(in srgb,var(--bc) 11%,transparent)}
vera-exploded .xp-lb.more{font-size:8.5px;letter-spacing:.04em;text-transform:none;transform:translate(-100%,-50%);pointer-events:auto;cursor:pointer}
vera-exploded .xp-lb.empty{font-size:8.5px;letter-spacing:.06em;text-transform:none;font-style:italic;opacity:.7;transform:translate(-50%,-50%)}
vera-exploded .xp-lb.lane{transform:translate(-100%,-50%)}vera-exploded .xp-lb.prompt{transform:translate(-50%,-100%)}
vera-exploded .xp-lb.sm,vera-exploded .xp-lb.station{transform-origin:0 0}
/* the galaxy lying on the plate — a sheet, counter-scaled with the items */
vera-exploded .xp-g.iso{background:transparent;box-shadow:none;transform:translate(-50%,-50%) scale(var(--inv,1));transform-origin:50% 50%;padding:0;overflow:visible;z-index:7}
/* Blocks off: plates, bands and cards lose their fills — outlines only (the board) */
:root[data-blocks="off"] vera-exploded .xp-pl,:root[data-blocks="off"] vera-exploded .xp-band{background:none!important}
:root[data-blocks="off"] vera-exploded .xp-pl.iso{box-shadow:none;outline:1px solid color-mix(in srgb,var(--xp-ac) 30%,transparent);outline-offset:-1px}
:root[data-blocks="off"] vera-exploded .xit,:root[data-blocks="off"] vera-exploded .xp-it,:root[data-blocks="off"] vera-exploded .xp-cp{background:transparent!important}
:root[data-blocks="off"] vera-exploded .xiw{opacity:.6}
/* the detail tiers, behaving as they do everywhere on the board: FULL — it is all on the card; HOVER — pointing reveals more;
   ZEN — hover does nothing at all, every level arrives on click */
vera-exploded[data-den="hover"] .xit{min-height:0}vera-exploded[data-den="hover"] .xit .xit-n{white-space:nowrap;display:block}vera-exploded[data-den="hover"] .xit:hover .xit-n,vera-exploded[data-den="hover"] .xit.open .xit-n{white-space:normal}
vera-exploded[data-den="hover"] .xit .xit-m,vera-exploded[data-den="hover"] .xit .xit-d,vera-exploded[data-den="hover"] .xit .xit-body{max-height:0;opacity:0;overflow:hidden;margin-top:0;transition:max-height .22s ease,opacity .22s ease}
vera-exploded[data-den="hover"] .xit:hover .xit-m,vera-exploded[data-den="hover"] .xit.open .xit-m,vera-exploded[data-den="hover"] .xit:hover .xit-d,vera-exploded[data-den="hover"] .xit.open .xit-d,vera-exploded[data-den="hover"] .xit:hover .xit-body,vera-exploded[data-den="hover"] .xit.open .xit-body{max-height:200px;opacity:1}
vera-exploded[data-den="zen"] .xit{min-height:0}vera-exploded[data-den="zen"] .xit .xit-n{white-space:nowrap;display:block}vera-exploded[data-den="zen"] .xit.open .xit-n{white-space:normal}vera-exploded[data-den="zen"] .xit .xit-d,vera-exploded[data-den="zen"] .xit .xit-m,vera-exploded[data-den="zen"] .xit .xit-body{display:none}
vera-exploded[data-den="zen"] .xit:hover{z-index:10;box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-bd)}vera-exploded[data-den="zen"] .xit.open .xit-d{display:block}vera-exploded[data-den="zen"] .xit.open .xit-m{display:flex}vera-exploded[data-den="zen"] .xit.open .xit-body{display:flex}
vera-exploded[data-den="zen"] .xnd:hover .xnl{display:none}vera-exploded[data-den="zen"] .xnd.sel .xnl{display:block}
/* the scene tips in when it opens and flattens back when it closes (3D → 2D), the transcript fading around it */
vera-exploded .xp-wrap{perspective:1400px}
vera-exploded.opening .xp-view{animation:xp-tip .5s cubic-bezier(.2,.8,.2,1) both}
vera-exploded.closing .xp-view{animation:xp-flat .24s ease-in both}
@keyframes xp-tip{from{opacity:0;transform:rotateX(-26deg) scale(.94)}to{opacity:1;transform:none}}
@keyframes xp-flat{from{opacity:1;transform:none}to{opacity:0;transform:rotateX(-26deg) scale(.96)}}
vera-exploded .xp-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--xp-t3);text-align:center;padding:20px;line-height:1.5}
`;
  function ensureCss(doc) { doc = doc || document; if (doc.getElementById('vera-exploded-css')) return; const s = doc.createElement('style'); s.id = 'vera-exploded-css'; s.textContent = CSS; (doc.head || doc.documentElement).appendChild(s); }
  // the shared ISO projection (/ui/iso.js, window.VeraISO) draws the widget groups; a page that has not loaded it gets it
  // once, here, and the scene redraws when it lands — without it the widgets stay flat cards (the board's widget card)
  function ensureIso(doc, onload) { doc = doc || document; if (root.VeraISO || doc.getElementById('vera-iso-lib')) return; const s = doc.createElement('script'); s.id = 'vera-iso-lib'; s.src = '/ui/iso.js'; s.async = true; s.onload = () => { try { onload && onload(); } catch (_) {} }; (doc.head || doc.documentElement).appendChild(s); }
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
  /* ── the ISO card's body, the board's vocabulary by kind: what shows on the card, and what waits behind a click ── */
  function isoBody(c, wd) {
    c = c || {}; const k = String(c.kind || '').toLowerCase(); let on = '', x = '';
    const codeish = /^(code|term|terminal|cap|capability|log)$/.test(k); const lines = String(c.body || '').split('\n').filter((l) => l.trim());
    if (c.score != null && !codeish && !/^(image|widget|artifact|note|loop|diff)$/.test(k)) on += '<span class="xf-score"><span class="xf-bar"><i style="width:' + Math.round(Math.max(0, Math.min(1, +c.score)) * 100) + '%"></i></span><b>' + (+c.score).toFixed(2) + '</b></span>';
    if (k === 'diff' && (c.p != null || c.m != null)) on += '<span class="xf-diff"><b class="p">+' + esc(c.p || 0) + '</b><b class="m">−' + esc(c.m || 0) + '</b></span>';
    if (Array.isArray(c.rows) && c.rows.length) { const tr = (r) => '<span class="xf-tr">' + esc(r.k) + '<b>' + esc(r.v) + '</b></span>'; on += '<span class="xf-tab">' + c.rows.slice(0, 3).map(tr).join('') + '</span>'; if (c.rows.length > 3) x += '<span class="xf-tab">' + c.rows.slice(3, 12).map(tr).join('') + '</span>'; }
    if (Array.isArray(c.steps) && c.steps.length) { const ls = (s, i) => '<span class="xf-ls ' + esc(s.status || '') + '"><i></i><b>' + (i + 1) + '</b><span>' + esc(s.label || s.n || '') + (s.cap ? ' · ' + esc(s.cap) : '') + '</span></span>'; on += '<span class="xf-loop">' + c.steps.slice(0, 3).map(ls).join('') + '</span>'; if (c.steps.length > 3) x += '<span class="xf-loop">' + c.steps.slice(3, 12).map((s, i) => ls(s, i + 3)).join('') + '</span>'; }
    if (Array.isArray(c.bars) && c.bars.length) { const vs = c.bars.map((b) => typeof b === 'number' ? b : parseFloat(b) || 0), mx = Math.max.apply(null, vs) || 1; on += '<span class="xf-chart">' + vs.slice(0, 12).map((v) => '<i style="height:' + Math.round(v / mx * 100) + '%"></i>').join('') + '</span>'; }
    if (wd && (k === 'widget' || c.form || c.tpl)) { // a widget without the iso lib: the board's widget card — the reading, its source, a sparkline
      const vs = seriesOf(wd), mx = Math.max.apply(null, vs.map((v) => Math.abs(v))) || 1; on += '<span class="xf-w"><b>' + esc(valueOf(wd) || (wd.sample ? '—' : '')) + '</b><span class="xf-wd">' + esc(wd.form) + (wd.sample ? ' · no reading yet' : '') + '</span></span>' + (vs.length > 1 ? '<span class="xf-ws">' + vs.map((v) => '<i style="height:' + Math.max(8, Math.round(Math.abs(v) / mx * 100)) + '%"></i>').join('') + '</span>' : ''); }
    if (codeish && lines.length) { on += '<span class="' + (k === 'code' ? 'xf-code' : 'xf-term') + '">' + esc(lines[0].slice(0, 80)) + '</span>'; if (lines.length > 1) x += '<pre>' + esc(String(c.body).slice(0, 600)) + '</pre>'; }
    else if (c.body && !c.rows) x += '<pre>' + esc(String(c.body).slice(0, 600)) + '</pre>';
    return { on, x };
  }

  if (typeof HTMLElement !== 'undefined' && root.customElements && !root.customElements.get('vera-exploded')) {
    class VeraExploded extends HTMLElement {
      constructor() { super(); this._S = { scene: { turns: [], sel: '' }, mode: 'cards', layer: 2, open: null, pan: { x: 0, y: 0, z: 1 } }; this._raf = 0; }
      connectedCallback() {
        ensureCss(this.ownerDocument); ensureIso(this.ownerDocument, () => this._schedule()); if (this._built) { this._schedule(); return; } this._built = true;
        const m = this.getAttribute('mode'); if (m) this._S.mode = m;
        this.innerHTML = '<div class="xp-ctl"><span class="c">explode</span><button data-m="cards">Cards</button><button data-m="front">Front</button><button data-m="iso">Iso</button><span class="sep"></span><button data-a="fit" title="Back to the whole scene">Fit</button><button data-a="close" title="Back to the flat transcript">Flatten</button><span class="sep"></span><button data-a="stack" title="Stack — the stations on floors, one above the other">Stack</button><button data-a="wsz" title="The iso widgets\' size — S · M · L">M</button><span class="sep"></span><button data-a="place" title="Place a widget from the registry onto this station\'s plate — it becomes one of the turn\'s items, tagged ⧉ with its template">+ Place</button><span class="sep"></span><input type="range" class="xp-scrub" data-r="scrub" min="0" max="0" value="0" title="Scrub through the session\'s turns (← → too)"></div><div class="xp-dots" data-r="dots"></div><div class="xp-wrap" data-r="wrap"><div class="xp-view" data-r="view"></div></div><div class="xp-pz"><button data-a="zout">−</button><span class="z" data-r="zoom">100%</span><button data-a="zin">+</button></div>';
        this._r = {}; this.querySelectorAll('[data-r]').forEach((el) => { this._r[el.dataset.r] = el; });
        this.addEventListener('click', (e) => this._click(e));
        // the timeline: the slider and ← → walk the session's turns
        if (!this.hasAttribute('tabindex')) this.setAttribute('tabindex', '0');
        if (this._r.scrub) this._r.scrub.addEventListener('input', () => { const ts = this._S.scene.turns || []; const t = ts[Math.max(0, Math.min(ts.length - 1, +this._r.scrub.value || 0))]; if (t && t.mid !== this._S.scene.sel) this.select(t.mid); });
        this.addEventListener('keydown', (ev) => { if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return; if (ev.target && /^(input|textarea)$/i.test(ev.target.tagName)) return; const ts = this._S.scene.turns || []; if (!ts.length) return; let i = ts.findIndex((t) => t.mid === this._S.scene.sel); i = Math.max(0, Math.min(ts.length - 1, i + (ev.key === 'ArrowRight' ? 1 : -1))); this.select(ts[i].mid); ev.preventDefault(); });
        const wrap = this._r.wrap;
        // the wheel zooms the whole scene about the pointer: plates, runs, cards, widgets and nodes grow together
        wrap.addEventListener('wheel', (e) => { if (this._S.mode === 'cards') return; e.preventDefault(); const p = this._S.pan; const nz = Math.max(0.4, Math.min(3, p.z * (e.deltaY > 0 ? 0.9 : 1.12))); const r = wrap.getBoundingClientRect(); const qx = e.clientX - (r.left + r.width / 2), qy = e.clientY - (r.top + r.height / 2); const k = nz / p.z; this._S.pan = { z: nz, x: qx - (qx - p.x) * k, y: qy - (qy - p.y) * k }; this._applyPan(); }, { passive: false });
        wrap.addEventListener('pointerdown', (e) => { if (e.button || this._S.mode === 'cards' || (e.target.closest && e.target.closest('.xp-it,.xp-rc,.xp-cp,.xit,.xig,.xnd,button,.xp-lb.station'))) return; this._drag = { x0: e.clientX, y0: e.clientY, px: this._S.pan.x, py: this._S.pan.y, id: e.pointerId, moved: false }; });
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
      // the pan zoom is one transform on the view: nothing counter-scales against it (the items' --inv follows the fit alone)
      _applyPan() { const p = this._S.pan; if (this._r.view) this._r.view.style.transform = this._S.mode === 'cards' ? 'none' : 'translate(' + p.x + 'px,' + p.y + 'px) scale(' + p.z + ')'; if (this._r.zoom) this._r.zoom.textContent = Math.round(p.z * 100) + '%'; }
      _click(e) {
        const t = e.target; const mb = t.closest && t.closest('button[data-m]'); if (mb) { this.mode(mb.dataset.m); return; }
        const ab = t.closest && t.closest('[data-a]'); if (ab) { const k = ab.dataset.a; if (k === 'fit') this.fit(); else if (k === 'stack') this.stack(); else if (k === 'wsz') this.widgetSize(); else if (k === 'place') { const ts = this._S.scene.turns || []; const t = ts.find((x) => x.mid === this._S.scene.sel) || ts[ts.length - 1]; this.dispatchEvent(new CustomEvent('vera:xpl:place', { detail: { mid: t ? t.mid : '' }, bubbles: true })); } else if (k === 'close') this.dispatchEvent(new CustomEvent('vera:xpl:close', { bubbles: true })); else if (k === 'zin' || k === 'zout') { const p = this._S.pan; p.z = Math.max(0.4, Math.min(3, p.z * (k === 'zin' ? 1.2 : 0.83))); this._applyPan(); } else if (k === 'prev' || k === 'next') { this._S.layer = Math.max(0, Math.min(LAYERS.length - 1, this._S.layer + (k === 'next' ? 1 : -1))); this._schedule(); } return; }
        const dot = t.closest && t.closest('.xp-dot'); if (dot) { this.select(dot.dataset.mid); return; }
        const st = t.closest && t.closest('.xp-lb.station'); if (st) { this.select(st.dataset.mid); return; }
        const ph = t.closest && t.closest('.xp-cp-h'); if (ph) { this._S.layer = +ph.closest('.xp-cp').dataset.li; this._schedule(); return; }
        const card = t.closest && t.closest('.xp-it,.xp-rc,.xit,.xig,.xnd'); if (card) { const id = card.dataset.id; this._S.open = this._S.open === id ? null : id; const [mid, layer] = id.split(':'); this._schedule(); this.dispatchEvent(new CustomEvent('vera:xpl:pick', { detail: { mid, layer, card: id }, bubbles: true })); if (layer === 'say') this.dispatchEvent(new CustomEvent('vera:xpl:turn', { detail: { mid }, bubbles: true })); return; }
        const cp = t.closest && t.closest('.xp-cp'); if (cp) { this._S.layer = +cp.dataset.li; this._schedule(); }
      }
      _schedule() { if (this._raf || !this._built) return; this._raf = (root.requestAnimationFrame || setTimeout)(() => { this._raf = 0; this._render(); }); }
      _render() {
        const S = this._S, wrap = this._r.wrap, view = this._r.view; const W = wrap.clientWidth || 800, H = wrap.clientHeight || 600;
        const ISO = root.VeraISO && typeof root.VeraISO.proj === 'function' ? root.VeraISO : null;
        const proj = ISO ? root.VeraISO.proj(30, 45, 1, true) : null;   // the shared projection when it is there; z in px, as the floors are measured
        const den = (this.ownerDocument && this.ownerDocument.documentElement.getAttribute('data-den') || 'full').toLowerCase(); this.dataset.den = den;
        const o = layout(S.scene, S.mode, W, H, { layer: S.layer, proj, den, stack: S.stack, wsz: S.wsz }); this._last = o;
        this.querySelectorAll('.xp-ctl button[data-a="stack"]').forEach((b) => { b.classList.toggle('on', !!S.stack && o.mode === 'iso'); b.style.display = o.mode === 'iso' ? '' : 'none'; });
        this.querySelectorAll('.xp-ctl button[data-a="wsz"]').forEach((b) => { b.textContent = (S.wsz || 'm').toUpperCase(); b.style.display = o.mode === 'iso' ? '' : 'none'; });
        if (this._r.scrub) { this._r.scrub.max = String(Math.max(0, (S.scene.turns || []).length - 1)); this._r.scrub.value = String(o.sel); }
        this.querySelectorAll('.xp-ctl button[data-m]').forEach((b) => b.classList.toggle('on', b.dataset.m === o.mode));
        this._r.dots.innerHTML = (S.scene.turns || []).map((t, i) => '<div class="xp-dot' + (i === o.sel ? ' on' : '') + '" data-mid="' + esc(t.mid) + '" title="' + esc((t.who || 'you') + ' · ' + (t.t || '') + ' · ' + String(t.text || '').slice(0, 80)) + '"><span class="l">' + esc((t.who || 'you') + ' ' + (i + 1) + ' · ' + String(t.text || '').slice(0, 30)) + '</span><i></i></div>').join('');
        view.classList.toggle('scroll', o.mode === 'cards');
        view.style.setProperty('--inv', o.mode === 'iso' ? String(o.inv || 1) : '1');   // the fit's counter-scale, once per render; the zoom never touches it
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
        // ISO: the board's card on its stem; a widget as an iso widget group with its frameless caption; a context record as a typed node
        const tplTag = (c) => (c.tpl ? '<i class="tpl" title="placed from the registry · ' + esc(c.tpl) + '">⧉</i> ' : '');
        const xitHtml = (wg) => { const c = wg.card, open = S.open === wg.id; const wd = { form: wg.form, data: wg.data, sample: wg.sample }; const b = isoBody(c, wd);
          return '<div class="xit bb' + (wg.tight ? ' tight' : '') + (open ? ' open' : '') + (c.src ? ' has-img' : '') + '" data-id="' + esc(wg.id) + '" title="' + esc(c.n || '') + (c.d ? ' — ' + esc(c.d) : '') + ' · click for the record" style="left:' + (wg.x - wg.cw / 2).toFixed(1) + 'px;top:' + (wg.y - wg.stem).toFixed(1) + 'px;width:' + wg.cw + 'px;--ih:' + wg.ch + 'px;--cc:' + esc(wg.col) + '">'
            + '<span class="xit-n">' + tplTag(c) + esc(c.n || '') + '</span><span class="xit-d">' + esc(c.d || '') + '</span>'
            + (b.on ? '<span class="xit-body">' + b.on + '</span>' : '') + (c.src ? '<img class="xp-img" src="' + esc(c.src) + '" alt="" loading="lazy">' : '') + (b.x ? '<div class="xit-x">' + b.x + '</div>' : '') + '</div>'; };
        const xigHtml = (wg) => { const c = wg.card, open = S.open === wg.id; const g = groupOf(wg, ISO, { tilt: 30, azim: 45 }); const b = isoBody(c, null);
          const cap = '<div class="xit frameless' + (open ? ' open' : '') + '" data-id="' + esc(wg.id) + '" title="' + esc(c.n || '') + (c.d ? ' — ' + esc(c.d) : '') + ' · click for the detail" style="left:' + (wg.x - wg.cw / 2).toFixed(1) + 'px;top:' + (wg.y + 6).toFixed(1) + 'px;width:' + wg.cw + 'px;--cc:' + esc(wg.col) + '"><span class="xit-n">' + tplTag(c) + esc(c.n || '') + '</span>' + (wg.value ? '<span class="xit-cv">' + esc(wg.value) + '</span>' : '') + '<span class="xit-d">' + esc(c.d || '') + '</span>'
            + '<div class="xit-x"><b style="color:var(--xp-t1)">' + esc(c.n || '') + '</b><br><span style="font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3)">' + esc(wg.form) + (wg.sample ? ' · no reading yet' : wg.value ? ' · ' + esc(wg.value) : '') + (c.tpl ? ' · ⧉ ' + esc(c.tpl) : '') + '</span>' + (b.on || b.x ? '<div class="xit-body" style="display:flex">' + b.on + b.x + '</div>' : '') + '</div></div>';
          if (!g) return xitHtml(wg);   // no iso lib on the page: the widget is the board's flat widget card
          return '<div class="xig' + (wg.sample ? ' sample' : '') + (open ? ' open' : '') + '" data-id="' + esc(wg.id) + '" title="' + esc(c.n || '') + ' · click for the detail" style="' + st(wg.x, wg.y) + '--wsh:' + (-g.sh).toFixed(1) + 'px;--cc:' + esc(wg.col) + '">'
            + g.faces.map((f) => '<i class="xiw ' + f.k + (f.cls ? ' ' + f.cls : '') + '" style="left:' + f.x + ';top:' + f.y + ';width:' + f.w + ';height:' + f.h + ';--cp:' + f.cp + ';--fc:' + f.col + '"></i>').join('')
            + (g.needle ? '<span class="xiw-n" style="left:' + g.needle.x + ';top:' + g.needle.y + ';width:' + g.needle.len + ';transform:rotate(' + g.needle.deg + ')"></span>' : '')
            + (g.big ? '<span class="xiw-b" style="left:' + g.big.x + ';top:' + g.big.y + '">' + esc(g.big.n) + '</span>' : '') + '</div>' + cap; };
        if (o.mode === 'iso') {
          this.classList.toggle('stacked', !!o.stack);
          o.plates.forEach((p) => { const xs = p.poly.map((q) => q.x), ys = p.poly.map((q) => q.y); const x0 = Math.min.apply(null, xs), y0 = Math.min.apply(null, ys), x1 = Math.max.apply(null, xs), y1 = Math.max.apply(null, ys); const cp = 'polygon(' + p.poly.map((q) => (q.x - x0).toFixed(1) + 'px ' + (q.y - y0).toFixed(1) + 'px').join(',') + ')'; h += '<div class="xp-pl iso' + (p.cls ? ' ' + p.cls : '') + '" style="' + st(x0, y0) + 'width:' + (x1 - x0).toFixed(1) + 'px;height:' + (y1 - y0).toFixed(1) + 'px;--cp:' + cp + '"></div>'; });
        } else { o.plates.forEach((p) => { h += '<div class="xp-pl' + (p.cls ? ' ' + p.cls : '') + '" style="' + st(p.x, p.y) + 'width:' + p.w + 'px;height:' + p.h + 'px"></div>'; }); }
        (o.bands || []).forEach((b) => { const xs = b.poly.map((q) => q.x), ys = b.poly.map((q) => q.y); const x0 = Math.min.apply(null, xs), y0 = Math.min.apply(null, ys), x1 = Math.max.apply(null, xs), y1 = Math.max.apply(null, ys); const cp = 'polygon(' + b.poly.map((q) => (q.x - x0).toFixed(1) + 'px ' + (q.y - y0).toFixed(1) + 'px').join(',') + ')'; h += '<div class="xp-band ' + esc(b.layer) + (b.empty ? ' empty' : '') + (b.cls ? ' ' + b.cls : '') + '" style="' + st(x0, y0) + 'width:' + (x1 - x0).toFixed(1) + 'px;height:' + (y1 - y0).toFixed(1) + 'px;--cp:' + cp + ';--bc:' + esc(b.col) + '"></div>'; });
        o.edges.forEach((e) => { h += '<div class="xp-e ' + e.cls + '" title="' + esc(e.title) + '" style="' + st(e.x, e.y) + 'width:' + e.len + 'px;--ec:' + esc(e.col) + ';transform:rotate(' + e.deg + 'deg)"></div>'; });
        o.labels.forEach((l) => { h += '<div class="xp-lb ' + l.cls + '" data-mid="' + esc(l.mid || '') + '" style="' + st(l.x, l.y) + 'color:' + esc(l.col) + (o.mode === 'iso' ? ';transform:' + (/\bmore\b/.test(l.cls) ? 'translate(-100%,-50%) ' : /\bempty\b/.test(l.cls) ? 'translate(-50%,-50%) ' : /\blane\b/.test(l.cls) ? 'translate(-100%,-50%) ' : /\bprompt\b/.test(l.cls) ? 'translate(-50%,-100%) ' : '') + 'scale(var(--inv,1))' : '') + '">' + esc(l.n) + '<b>' + esc(l.k) + '</b></div>'; });
        o.cards.forEach((c) => { if (!c.iw) h += itHtml(c, !!c.anchored); });
        o.graphs.forEach((g) => { h += g.iso
          ? '<div class="xp-g iso" data-id="' + esc(g.id) + '" style="' + st(g.x, g.y) + 'width:' + g.w + 'px;height:' + g.h + 'px">' + (root.VeraWidget && typeof root.VeraWidget.draw === 'function' ? root.VeraWidget.draw('context_graph', { nodes: g.data.nodes, rels: g.data.rels }, 'm', { height: g.h, width: g.w, bare: true, view: 'iso', full: false, hubMeta: g.data.nodes.length + ' rec' }) : graphHtml(g.data, g.h)) + '</div>'
          : '<div class="xp-g" data-id="' + esc(g.id) + '" style="' + st(g.x, g.y) + 'width:' + g.w + 'px;height:' + g.h + 'px">' + graphHtml(g.data, g.h - 10) + '</div>'; });
        (o.gnodes || []).forEach((n) => { h += '<span class="xnd' + (n.lit ? ' lit' : '') + (n.ghost ? ' ghost' : '') + (S.open === n.id ? ' sel' : '') + '" data-id="' + esc(n.id) + '" title="' + esc(n.label + ' — ' + n.lane + ' · relevance ' + n.score.toFixed(2) + (n.ghost ? ' · related, not injected' : ' · in the prompt')) + '" style="' + st(n.x, n.y) + 'width:' + n.d + 'px;height:' + n.d + 'px;--nc:' + esc(n.col) + ';opacity:' + n.op + '"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"><path d="' + n.icon + '"></path></svg><span class="xnl">' + esc(n.label) + '<b>' + esc(n.lane + ' · ' + n.score.toFixed(2)) + '</b></span></span>'; });
        // the stems first, then every item in paint order — the lower on the screen, the later (it stands in front)
        (o.widgets || []).forEach((wg) => { h += '<span class="xstem" style="left:' + wg.x.toFixed(1) + 'px;top:' + (wg.y - (wg.draw === 'group' ? 0 : wg.stem)).toFixed(1) + 'px;height:' + (wg.draw === 'group' ? 0 : wg.stem) + 'px;--sc:' + esc(wg.col) + '"><i></i></span>'; });
        (o.widgets || []).slice().sort((a, b) => a.y - b.y).forEach((wg) => { h += wg.draw === 'group' ? xigHtml(wg) : xitHtml(wg); });
        if (o.mode === 'cards') h = '<div style="position:relative;width:' + o.size.w + 'px;height:' + o.size.h + 'px">' + h + '</div>';
        view.innerHTML = h; this._applyPan(); this._emit(o);
      }
      _emit(o) { this.dispatchEvent(new CustomEvent('vera:xpl:rendered', { detail: { mode: o.mode, stations: o.stations, cards: o.cards.length, panels: o.panels.length, graphs: (o.graphs || []).length, gnodes: (o.gnodes || []).length, inv: o.inv || 1 }, bubbles: true })); }
    }
    root.customElements.define('vera-exploded', VeraExploded);
  }
  const api = { layout, LAYERS, ensureCss, ensureIso, graphData, widgetOf, groupOf, valueOf, isoBody, ICON, version: 4 };
  root.VeraExploded = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
