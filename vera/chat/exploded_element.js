/* The exploded scene — the chat's Explode (UI redesign: the Chat & canvas set, Notes/40 §6 P6; the board's
   "ONE EXPLODED SCENE"): not three panes side by side but one pipeline for the session — per turn (a station) what
   the turn READ, the EXCHANGE, what it PRODUCED and where it LANDED on the canvas — with the runs drawn between the
   actual entities. Cards · Front · Iso are three projections of this one scene, not three re-implementations:
     cards — the board's tri-page cards: the five layers side by side as columns at 1:1, a plate each, the cards on a
             row pitch with a tall one pushing the rows under it down, the runs level out of a card's side and down the
             gutter — never diagonally, always behind the cards; every turn is such a row, the stage scrolls both ways;
     front — the selected station as a carousel of its layers in depth (the same construction the board uses);
     iso   — the lattice: u = station, v = the layer bands, the plates identical parallelograms in a row, projected
             through the shared ISO projection (/ui/iso.js) when it is there, the classic 30°/45° otherwise.
   Nothing here uses CSS 3D except the front carousel; plates, cards and runs sit at screen coordinates from one
   projection, so a run ends on a card, not near it. The composer stays docked under the scene (the host's).

   The iso's items are drawn the way the Canvas board draws its exploded iso (Canvas.dc.html, the .xit / .xig / .xnd
   vocabulary): every item is the board's CARD standing on a stem at its pin — name · value · meta · a small body by
   kind (a score bar, chart bars, a diff, table rows, the loop's steps, a line of code or terminal, a diagram drawn by
   the estate's mermaid element); a widget (placed from the registry through the WidgetConfig sheet, or one the reply
   carried) is drawn as ITS OWN FORM — the registry's renderer (VeraWidget.draw, the one drawer every placement uses)
   at the scene's widget size, the form's own sample face until it reads — standing on its stem as the board's card;
   without the widget element on the page it is the ISO WIDGET GROUP — a dial, bars or a block built through the ISO
   lib's box/face/scene — with a frameless caption beneath it (the card is only its label); the records in the
   context-graph band are typed ICON NODES lying on the plate about the prompt line, the galaxy sheet past them.
   The items counter-scale by the FIT only (1:1 text when the scene is fitted, capped as the board's embed is), so
   the wheel / ± zoom grows and shrinks them WITH the scene.

   <vera-exploded>  API: setScene({turns:[{mid, who, t, text, reply, read:[card], say:[card], made:[card],
                    land:[card], activity?:[call], estate?:[node]}], sel}) · mode(name) · select(mid) · fit() · state() · solo(on) · stack(on) · tilt(deg) · swing(deg) · pan(dx, dy)
                    · layers({activity, estate}) · galaxy(on)
   card = {n, d, col, kind, body?, score?, p?, m?, rows?, form?, data?, record? (a placed widget's record: form · source · frame · draw · data), mermaid? (a diagram's source)}
   a turn IN PROGRESS: turn.pending:true (+ turn.phrase, the thinking phrase the chat rolls) — or a card of kind 'pending'
   (its n / phrase the phrase) — draws the GENERATING state on the exchange in every mode and never a card; the chat's
   streaming placeholder text ("Generating…", "Thinking…", an ellipsis) is dropped as a card too. It clears when the reply lands.
   events: vera:xpl:pick {mid, layer, card} · vera:xpl:turn {mid} · vera:xpl:rendered {mode, stations} · vera:xpl:place {mid}
           · vera:xpl:layers {activity, estate, galaxy} · vera:xpl:move {id, key, from, to, before} (a widget picked up and dropped) · vera:xpl:edit {id, key, mid, card}
   The RUNS: every run is orthogonal — level and plumb in cards, along the lattice's u, v and z in iso — collected first and
   laned afterwards (cardsRouter · isoRouter), so no two legs share a length and a bundle stays parallel. The ACTIVITY layer
   hangs the turn's capability calls off the card that triggered each; the ESTATE layer draws where they ran. Both are
   optional and remembered; so is the mini galaxy on the graph band (off: the board's lanes alone).
   window.VeraExploded = { layout, frontRuns, LAYERS, version } — layout() and frontRuns() are pure (node-testable). */
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
  // relations INSIDE the context graph, as the board colours them: what cites what (thin), which memory relates (dashed), the loop's own order
  const RELC = { cite: ['var(--xp-t3)', 'rel cite', 'cites'], mem: ['var(--xp-ac2)', 'rel mem', 'a memory that relates'], step: ['var(--xp-ac)', 'rel step', 'the next step in the loop'] };
  // the carousel's runs by kind: what was read in, what came out, what became a canvas item, what was pinned back
  const FRC = { in: 'var(--xp-dv1)', out: 'var(--xp-dv2)', link: 'var(--xp-ac)', mem: 'var(--xp-ac2)' };
  /* the galaxy sheet lying past the nodes. The galaxy is a ROUND plot — its own mini is laid out at 262x196 — so a
     sheet much wider than it is tall leaves records outside it top and bottom, and the sheet clips them; a sheet that
     small also leaves the plot using a third of it. The sheet takes its height from its width at the plot's proportion,
     so the whole galaxy is inside it and as big as the band can carry (Notes/42 defect 61). */
  const galH = (w) => Math.round(Math.max(110, Math.min(240, w * 0.66)));

  /* ── the layout, pure ─────────────────────────────────────────────────────────────────────────────── */
  function layout(scene, mode, W, H, o) {
    o = o || {}; let turns = (scene && scene.turns) || []; mode = mode === 'front' || mode === 'iso' ? mode : 'cards';
    const selIdx = Math.max(0, turns.findIndex((t) => t.mid === (scene && scene.sel))); let sel = turns.length ? Math.max(0, selIdx) : 0;
    const out = { mode, stations: turns.length, sel, plates: [], labels: [], cards: [], edges: [], panels: [], leaders: [], graphs: [], gnodes: [], size: { w: W, h: H }, fit: { s: 1, x: 0, y: 0 }, inv: 1 };
    const den = o.den === 'hover' || o.den === 'zen' ? o.den : 'full';
    // an image travels with its card: Full gives it room on the card; Hover and Zen show it over the card instead
    const chOf = (c, CH) => (c && c.src && den === 'full') ? CH + 62 : CH;
    // a turn in progress: the feed says so (turn.pending + turn.phrase), or a card of kind 'pending' does; the chat's
    // streaming placeholder is never a card (the reply text is dropped while it is only the placeholder)
    const PLACEHOLDER = /^(generating|thinking|working|streaming|writing)?\s*(…|\.\.\.)?\s*$/i;
    const isPend = (c) => !!c && /^(pending|gen)$/i.test(String(c.kind || ''));
    const pendOf = (t) => { const pc = [].concat(t.say || [], t.made || []).find(isPend); if (!t.pending && !pc) return null; return { mid: t.mid, phrase: String(t.phrase || (pc && (pc.phrase || pc.n)) || '').replace(PLACEHOLDER, (m) => m ? '' : m).trim() || 'Generating…' }; };
    const genCard = (t) => { const p = pendOf(t); return p ? [{ n: p.phrase, d: 'aide · generating', col: 'var(--xp-ac2)', kind: 'gen' }] : []; };
    const cardsOf = (t, k) => { if (k === 'say') { const own = (t.say || []).filter((c) => !isPend(c) && !PLACEHOLDER.test(String(c.n || ''))); const reply = t.reply && !PLACEHOLDER.test(String(t.reply)) && !t.pending ? [{ n: t.reply, d: 'aide' + (t.rt ? ' · ' + t.rt : ''), col: 'var(--xp-ac)', kind: 'note' }] : [];
        return (own.length ? own : [{ n: t.text || '', d: (t.who || 'you') + (t.t ? ' · ' + t.t : ''), col: 'var(--xp-ac)', kind: 'note' }].concat(reply)).concat(genCard(t)); }
      return (t[k] || []).filter((c) => !isPend(c)); };
    out.pending = turns.map(pendOf).filter(Boolean);
    const edge = (a, b, col, cls, title) => { const dx = b.x - a.x, dy = b.y - a.y; out.edges.push({ x: px(a.x), y: px(a.y), len: px(Math.sqrt(dx * dx + dy * dy)), deg: +(Math.atan2(dy, dx) * 180 / Math.PI).toFixed(2), col, cls: cls || '', title: title || '' }); };
    if (!turns.length) return out;
    // the chip bar's filters over the context graph: a lane the user hid, and the related-but-not-injected records
    // shown or not (the board's `related`); the bar itself reads the selected turn's UNFILTERED graph for its counts
    const LOFF = o.lanesOff || {}, RELON = o.related !== false;
    const gData = (t) => { const g = graphData(t); if (!Object.keys(LOFF).some((k) => LOFF[k]) && RELON) return g;
      const nodes = g.nodes.filter((n) => !LOFF[n.lane] && (RELON || n.included !== false)), ids = {}; nodes.forEach((n) => { ids[n.id] = 1; });
      const laneList = LANES.filter((l) => nodes.some((n) => n.lane === l)); return { nodes, rels: g.rels.filter((r) => ids[r.from] && ids[r.to]), lanes: laneList.length, laneList }; };
    { const g0 = graphData(turns[sel]); out.ctx = { lanes: LANES.filter((l) => g0.nodes.some((n) => n.lane === l)).map((l) => ({ lane: l, n: g0.nodes.filter((n) => n.lane === l && (RELON || n.included !== false)).length, off: !!LOFF[l], col: (g0.nodes.find((n) => n.lane === l && n.col) || {}).col || LAYERS[0].col })), ghosts: g0.nodes.filter((n) => n.included === false).length, related: RELON, acts: actsOf(turns[sel]).length, ests: estOf(turns[sel]).length }; }
    if (mode === 'cards') {
      // the board's CARDS scene (Canvas.dc.html, the tri-page's cards): the five layers stand side by side as columns —
      // a rounded plate each with its caption inside the top edge, sized to ITS content (the exchange tall, a short
      // layer short, the graph plate to its lanes); the cards dense (54 on a 64 row pitch: name · mono meta with the
      // relevance as a short bar and its number · a one-line body), a tall card pushing the rows under it down by its
      // measured excess (the element measures every card after a render and lays out once more; a guess by kind until
      // then); the context graph a lane per family of typed nodes across its plate — the galaxy sheet past them only
      // when the mini graph is asked for (the board draws the lanes alone). The five plates FIT the view's width — the
      // board's rule for the embedded scene, "the stations side by side across the width; the height pans" — by their
      // geometry (plate, gutter and card widths scale, text stays 1:1); below half the board's width the stage scrolls
      // sideways. Every turn is such a row, in order; the selected lit. The ACTIVITY layer hangs the turn's capability
      // calls off the card that triggered each of them under the produced cards; the ESTATE layer is a sixth plate.
      const ACT = !!(o.layers && o.layers.activity), EST = !!(o.layers && o.layers.estate), GAL = o.galaxy !== false;   // the board draws the galaxy sheet beside the lanes; the chip hides it
      const ESTL = { key: 'estate', name: 'estate', sub: 'where it ran', col: 'var(--xp-dv3)', kind: 'nodes' };
      const COLSL = EST ? LAYERS.concat([ESTL]) : LAYERS, NP = COLSL.length;
      const kW = Math.max(0.5, Math.min(1, (W - 96) / (NP * 430 + (NP - 1) * 54)));   // the margins, and room for the rounding
      const SWD = Math.round(430 * kW), SGP = Math.round(54 * kW), CW = SWD - 32, NX = Math.round(120 * kW), VP = 64, CH = 54, PADX = 40, PADY = 92, ROWGAP = 60, NR = 44, HEAD = 60, FOOT = 24, MINH = 150;
      const HM = o.heights || {};
      const idOf = (c) => String((c && (c.id != null ? c.id : c.n)) || '');
      const estH = (c) => { const k = String(c.kind || '').toLowerCase(); if (c.src && den === 'full') return CH + 62; if (Array.isArray(c.steps) && c.steps.length) return 128; if (Array.isArray(c.rows) && c.rows.length) return 96; if (Array.isArray(c.bars) && c.bars.length) return 84; if (/^(code|term|terminal|cap|capability|log)$/.test(k)) return 80; if (k === 'diff') return 72; if (k === 'widget' || c.form || c.tpl || c.record) return 128; if (k === 'diagram') return 200; return CH; };
      const xU = (i) => PADX + 16 + i * (SWD + SGP);
      const laneH = (n, per) => n ? 40 + Math.ceil(n / per) * NR : 0;
      out.rows = []; out.runs = 0; out.geom = { kW, plate: SWD, gutter: SGP, card: CW, pitch: VP, ch: CH }; out.anodes = []; out.enodes = [];
      let rowTop = PADY;
      turns.forEach((t, si) => {
        const g = gData(t), lit = si === sel, boxes = {}; const oy = rowTop + 22 + HEAD;   // the row line of the first card, under the turn's caption and the plate's head
        const lanes = LANES.filter((l) => g.laneList.indexOf(l) >= 0);
        const members = (l) => g.nodes.filter((n) => n.lane === l).sort((a, b) => b.score - a.score);
        const acts = ACT ? actsOf(t) : [], ests = EST ? estOf(t) : [];
        const graphH = lanes.reduce((s, l) => s + 40 + Math.ceil(members(l).length / 3) * NR, 0) + (g.nodes.length && GAL ? galH(CW) + 30 : 0);
        // every column's rows, each row's excess over the base card, and from those the plate's height — its content's
        const cols = COLSL.map((L, i) => { if (L.kind === 'graph') return { L, i, list: [], extras: [], h: Math.max(MINH, HEAD + graphH + FOOT) };
          if (L.kind === 'nodes') return { L, i, list: [], extras: [], h: Math.max(MINH, HEAD + laneH(ests.length, 3) + FOOT) };
          const list = cardsOf(t, L.key); const extras = list.map((c, j) => Math.max(0, (HM[t.mid + ':' + L.key + ':' + j] || estH(c)) - CH));
          const cardsH = list.length ? (list.length - 1) * VP + CH + extras.reduce((a, b) => a + b, 0) : 0;
          return { L, i, list, extras, h: Math.max(MINH, HEAD + cardsH + (L.key === 'made' && acts.length ? 40 + actTree(acts, 6).rows.length * NR + (list.length ? 10 : 0) : 0) + FOOT) }; });
        const rowH = Math.max.apply(null, cols.map((c) => c.h));
        out.labels.push({ si, mid: t.mid, x: px(PADX), y: px(rowTop), n: (t.who || 'you') + ' · ' + (t.t || ''), k: String(t.text || '').slice(0, 60), cls: 'station' + (lit ? ' on' : ''), col: 'var(--xp-t2)' });
        cols.forEach((c) => { const x = xU(c.i);
          out.plates.push({ si, mid: t.mid, st: c.i, layer: c.L.key, x: px(x - 16), y: px(rowTop + 22), w: SWD, h: px(c.h), col: c.L.col, cls: 'rect' + (lit ? ' on' : '') + (c.L.key === 'say' && pendOf(t) ? ' gen' : '') });
          out.labels.push({ si, mid: t.mid, st: c.i, x: px(x - 4), y: px(rowTop + 22 + 15), n: c.L.name, k: c.L.sub, cls: 'layer hit ' + c.L.key + (lit ? ' on' : ''), col: c.L.col }); });
        // the cards: from the plate's head down on the row pitch; the rows under a tall one move down by its excess
        cols.forEach((c) => { if (c.L.kind) return; const x = xU(c.i); let voff = 0;
          let flow = 0;   // items dropped somewhere on the plate (card.at.cards) sit there; the rest flow on the pitch
          c.list.forEach((card, j) => { const xh = c.extras[j], h = CH + xh, id = t.mid + ':' + c.L.key + ':' + j; const at = c.L.key === 'land' && card.at && card.at.cards; let cy, cx0 = x;
            if (at && isFinite(+at.y)) { cy = rowTop + 22 + Math.max(HEAD + CH / 2, Math.min(c.h - FOOT - CH / 2, +at.y)); } else { cy = oy + flow * VP + voff; flow++; voff += xh; }
            out.cards.push({ id, mid: t.mid, si, layer: c.L.key, ci: j, x: px(cx0), y: px(cy - CH / 2), w: CW, h: px(h), ih: CH, card, col: card.col || c.L.col, ct: true, chip: c.L.key === 'land' ? 'canvas item' : c.L.name, badge: card.badge || (card.tpl ? 'placed' : card.included === false ? 'related' : ''), turn: (t.who || 'you') + ' · ' + (t.t || ''), drag: c.L.key === 'land' && isWidgetCard(card), key: card.key || '' });
            boxes[c.L.key + ':' + j] = { cx: cx0 + CW / 2, cy: cy - CH / 2 + h / 2, w: CW, h, st: c.i }; }); });
        // a lane of typed nodes across a plate column, three to a row (the board's context lanes; the activity and the estate use the same construction)
        const nodeLane = (colI, ly, ms, mk) => { const x = xU(colI); ms.forEach((n, mi) => { const row = Math.floor(mi / 3), inRow = Math.min(3, ms.length - row * 3), k = mi % 3; const cx = x + SWD / 2 + (k - (inRow - 1) / 2) * NX, cy = ly + row * NR + 12; mk(n, mi, cx, cy); }); return ly + Math.ceil(ms.length / 3) * NR; };
        // the context graph: a lane per family down the plate (its label at the plate's edge, with the count), the members
        // across the lane three to a row, the most relevant first; a node is bigger the more relevant it is
        { const x = xU(0); let ly = oy;
          lanes.forEach((lane) => { const ms = members(lane); out.labels.push({ si, mid: t.mid, x: px(x), y: px(ly - 13), n: lane, k: String(ms.length), cls: 'layer sm lane ' + lane, col: ms[0].col || LAYERS[0].col });
            ly = 40 + nodeLane(0, ly, ms, (n, mi, cx, cy) => { const rel = Math.max(0, Math.min(1, n.score)), d = Math.round(19 + rel * 9);
              out.gnodes.push({ id: t.mid + ':graph:' + n.id, nid: n.id, mid: t.mid, si, x: px(cx), y: px(cy), d, col: n.col || LAYERS[0].col, icon: ICON_OF(n.kind, n.lane), label: n.label, lane: n.lane, kind: n.kind, score: rel, ghost: n.included === false, lit: rel > 0.82, op: +(0.45 + rel * 0.55).toFixed(2) });
              boxes['node:' + n.id] = { cx, cy, w: d, h: d, st: 0, lane: n.lane, node: true }; }); });
          // the galaxy sheet lies past the lanes — the same widget the Context menu draws — when it is asked for
          if (g.nodes.length && GAL) out.graphs.push({ id: t.mid + ':graph', mid: t.mid, si, x: px(x), y: px(ly + 4), w: CW, h: galH(CW), data: g }); }
        // ACTIVITY: the calls this turn made, a lane of typed nodes under the produced cards, in the order they ran
        if (acts.length) { const c3 = cols[3]; let ly = oy + (c3.list.length ? (c3.list.length - 1) * VP + CH + c3.extras.reduce((a, b) => a + b, 0) + 10 : 0);
          out.labels.push({ si, mid: t.mid, x: px(xU(3)), y: px(ly - 13), n: 'activity', k: acts.length + ' call' + (acts.length === 1 ? '' : 's'), cls: 'layer sm lane activity', col: 'var(--xp-ac2)' });
          const tree = actTree(acts, 6), x3 = xU(3), per = Math.max(1, Math.min(6, Math.max.apply(null, tree.rows.map((r) => r.length).concat([1])))), gx = Math.min(NX, (SWD - 40) / per);
          tree.rows.forEach((row, ri) => { row.forEach((ai, k) => { const a = acts[ai], cx = x3 + SWD / 2 + (k - (row.length - 1) / 2) * gx, cy = ly + ri * NR + 12, d = 22;
            out.anodes.push({ id: t.mid + ':act:' + ai, mid: t.mid, si, x: px(cx), y: px(cy), d, col: actCol(a), icon: ICON.cap, label: a.n || a.cap || 'call', meta: actMeta(a), status: String(a.status || ''), lane: 'activity', depth: ri }); boxes['act:' + ai] = { cx, cy, w: d, h: d, st: 3, node: true }; }); }); }
        // ESTATE: where the turn's calls ran — the subsystems and the machines behind them, a sixth plate
        if (ests.length) { nodeLane(5, oy, ests, (e, ei, cx, cy) => { const d = 24; out.enodes.push({ id: t.mid + ':est:' + ei, mid: t.mid, si, x: px(cx), y: px(cy), d, col: estCol(e), icon: ICON_OF(e.kind, 'estate'), label: e.label || e.id, meta: e.detail || e.kind || '', status: String(e.status || ''), lane: 'estate' }); boxes['est:' + ei] = { cx, cy, w: d, h: d, st: 5, node: true }; }); }
        // the runs, routed as the board routes them: level out of a card's side, down or up the gutter between the
        // stations, level into the target — never diagonally. They are COLLECTED first and laned afterwards: every run
        // through a gutter has a lane of its own and every run leaving or entering a card a port of its own, ordered
        // by where they are going, so no two legs share a length and neighbouring runs stay parallel.
        const R = cardsRouter({ xU, SWD, SGP, kW, rowBottom: rowTop + 22 + rowH, edge, out });
        const bx = (k) => boxes[k], nb = (c) => boxes['node:' + idOf(c)];
        const reads = cardsOf(t, 'read'), mades = cardsOf(t, 'made'), lands = cardsOf(t, 'land');
        reads.forEach((c, j) => { const n = nb(c); if (n) R.add(n, bx('read:' + j), n.lane === 'memory' ? 'var(--xp-ac2)' : 'var(--xp-dv1)', n.lane === 'memory' ? 'in mem' : 'in', n.lane === 'memory' ? 'the memory that was recalled' : 'the context entry that was injected'); });
        const laneOfCard = (c) => { const nn = g.nodes.find((x) => x.id === idOf(c)); return nn ? nn.lane : 'context'; };
        const ex = bx('say:0'); if (ex) { reads.forEach((c, j) => R.add(bx('read:' + j), ex, 'var(--xp-dv1)', 'in', 'read by this turn', null, { trunk: 'in:' + laneOfCard(c), tend: 'b' })); if (!reads.length && lanes.length) R.add(nb(members(lanes[0])[0]), ex, 'var(--xp-dv1)', 'in', 'the context this turn read'); mades.forEach((c, j) => R.add(ex, bx('made:' + j), 'var(--xp-dv2)', 'out', 'produced by this turn', null, { trunk: 'out', tend: 'a' })); }
        // every canvas item is joined: to the product it came from (its key, else its place), or to the exchange it was placed on
        landRuns(mades, lands, (mi, li, why) => R.add(mi == null ? ex : bx('made:' + mi), bx('land:' + li), mi == null ? 'var(--xp-ac)' : 'var(--xp-ac)', mi == null ? 'link dash' : 'link', why));
        lands.forEach((c, j) => { const n = nb(c); if (n && n.lane === 'canvas') R.add(bx('land:' + j), n, 'var(--xp-ac2)', 'pin', 'pinned back into the next prompt'); });
        // the activity: off the capability card that triggered it (by name), else the exchange; then the chain, call to call
        actRuns(acts, mades, (from, ai, cls, title) => R.add(from === 'ex' ? ex : from[0] === 'made' ? bx('made:' + from[1]) : bx('act:' + from[1]), bx('act:' + ai), 'var(--xp-ac2)', cls, title, null, actTrunk(from, cls)));
        // the estate: every call to the subsystem it ran through, the machines behind it
        ests.forEach((e, ei) => { estRuns(e, (ai) => bx('act:' + ai)).forEach(([ai, title]) => R.add(bx('act:' + ai), bx('est:' + ei), 'var(--xp-dv3)', 'est', title, null, { trunk: 'est:' + ei, tend: 'b' })); if (e.via != null && bx('est:' + e.via)) R.add(bx('est:' + e.via), bx('est:' + ei), 'var(--xp-dv3)', 'rel est dash', (e.label || e.id) + ' serves it'); });
        // the RELATIONS between the turn's context records - the only runs that answer to the tier and the switch
        g.rels.forEach((r) => { const a = boxes['node:' + r.from], b = boxes['node:' + r.to], Rc = RELC[r.kind] || RELC.cite; if (a && b) R.add(a, b, Rc[0], Rc[1], Rc[2], [String(r.from), String(r.to)]); });
        R.flush();
        out.rows.push({ mid: t.mid, si, y: px(rowTop), h: px(rowH + 22) });
        rowTop += 22 + rowH + ROWGAP;
      });
      out.size = { w: xU(NP - 1) - 16 + SWD + PADX, h: rowTop - ROWGAP + PADY - 40 };
      out.inv = 1;
      return out;
    }
    if (mode === 'front') {
      // FRONT: the chat UI's carousel construction, ported rather than imitated — the selected station's layers pulled
      // apart along one axis, the one you are on nearest and centred, the rest receding either side, every face turned
      // the same little way off axis so each is read head-on. A panel is sized from the room the view has (the board's
      // 568 × 640 at its 2560 width); the layers past the window fade to a ghost and carry no cards. A wider gap where
      // the chat's own layers end and the context graph / session canvas begin. Clicking a layer's header FOCUSES it —
      // square-on, full size, the rest hushed — and again puts it back in the line.
      const t = turns[sel], g = gData(t);
      const PW = Math.round(Math.max(400, Math.min(568, (W - 40) / 2.6))), PH = Math.round(Math.max(420, Math.min(640, H - 200)));   // the runs' floor lies below the panels and the host's composer floats over the foot
      const PDX = Math.max(300, Math.min(520, (W - 80) / 3.2)), PDZ = 126, li0 = o.layer == null ? 2 : o.layer, foc = o.focus == null ? null : o.focus;
      const CAX = LAYERS.map((_, i) => i + (i >= 1 ? 0.3 : 0) + (i >= 4 ? 0.3 : 0));
      const idOf = (c) => String((c && (c.id != null ? c.id : c.n)) || '');
      // the context graph's layer is a card per family: the lane, its count and top relevance, its members as rows
      const lanes = LANES.filter((l) => g.laneList.indexOf(l) >= 0);
      const laneCards = lanes.map((l) => { const ms = g.nodes.filter((n) => n.lane === l).sort((a, b) => b.score - a.score); return { n: l, d: ms.length + ' · top ' + ms[0].score.toFixed(2), col: ms[0].col || LAYERS[0].col, kind: 'panel', lane: l, rows: ms.slice(0, 6).map((m) => ({ k: m.label, v: m.score.toFixed(2) })) }; });
      LAYERS.forEach((L, li) => { const off = CAX[li] - CAX[li0], a = Math.abs(li - li0); const list = L.kind === 'graph' ? laneCards : cardsOf(t, L.key);
        out.panels.push({ layer: L.key, name: L.name, sub: L.sub, col: L.col, li, n: list.length, w: PW, h: PH, px: off * PDX, pz: -Math.abs(off) * PDZ, gen: L.key === 'say' && !!pendOf(t),
          tf: foc === li ? 'translateX(0px) translateZ(24px) rotateY(0deg)' : 'translateX(' + (off * PDX).toFixed(0) + 'px) translateZ(' + (-Math.abs(off) * PDZ).toFixed(0) + 'px) rotateY(26deg)',
          cls: (foc === li ? 'focus ' : foc != null ? 'hushed ' : '') + (a === 0 ? 'on' : a === 1 ? 'near' : a >= 3 ? 'gone' : 'far') + (list.length > 5 ? ' many' : ''), d: (a * 0.07).toFixed(2) + 's', el: 1 + li, focL: foc === li ? 'back' : 'focus',
          cards: (a > 2 ? [] : list).map((c, ci) => ({ id: t.mid + ':' + L.key + ':' + (L.kind === 'graph' ? c.lane : ci), mid: t.mid, layer: L.key, ci, card: c, col: c.col || L.col })), graph: L.kind === 'graph' ? g : null }); });
      for (let i = 0; i + 1 < LAYERS.length; i++) { const oa = CAX[i] - CAX[li0], ob = CAX[i + 1] - CAX[li0]; const ax = oa * PDX, az = -Math.abs(oa) * PDZ, bx = ob * PDX, bz = -Math.abs(ob) * PDZ; out.leaders.push({ tf: 'translateX(' + ax.toFixed(0) + 'px) translateZ(' + az.toFixed(0) + 'px) rotateY(' + (Math.atan2(-(bz - az), bx - ax) * 180 / Math.PI).toFixed(1) + 'deg)', w: Math.sqrt((bx - ax) * (bx - ax) + (bz - az) * (bz - az)).toFixed(0) + 'px', cls: (i === 0 || i === 3) ? 'across' : 'same' }); }
      // the relations, the same ones the iso scene draws, as panel:card pairs — laid out for the carousel by frontRuns
      // once the element has measured where the cards sit: the context entry into the record it became (a memory's
      // dashed), every read into the exchange, the exchange into what it made, what it made into where it landed, and
      // a canvas item pinned back into the next prompt
      const nodeLane = {}; g.nodes.forEach((n) => { nodeLane[n.id] = n.lane; });
      const rels = [], R = (a, b, kind, title) => rels.push({ a, b, kind, col: FRC[kind], title });
      const reads = cardsOf(t, 'read'), says = cardsOf(t, 'say'), mades = cardsOf(t, 'made'), lands = cardsOf(t, 'land');
      // four of a kind at most: the carousel draws the thread, the cards and the iso draw every run
      reads.slice(0, 4).forEach((c, j) => { const ln = nodeLane[idOf(c)], k = ln ? lanes.indexOf(ln) : -1; if (k >= 0) R([0, k], [1, j], ln === 'memory' ? 'mem' : 'in', ln === 'memory' ? 'the memory that was recalled' : 'the context entry that was injected'); });
      if (says.length) { reads.slice(0, 4).forEach((c, j) => R([1, j], [2, 0], 'in', 'the passage the answer is based on')); if (!reads.length && lanes.length) R([0, 0], [2, 0], 'in', 'the context this turn read'); mades.slice(0, 4).forEach((c, j) => R([2, 0], [3, j], 'out', 'produced by this turn')); }
      mades.slice(0, 4).forEach((c, j) => { if (lands[j] || lands[0]) R([3, j], [4, lands[j] ? j : 0], 'link', 'this became a canvas item'); });
      lands.forEach((c, j) => { if (nodeLane[idOf(c)] === 'canvas' && lanes.indexOf('canvas') >= 0) R([4, j], [0, lanes.indexOf('canvas')], 'mem', 'pinned back into the next prompt'); });
      out.frontRels = rels; out.layer0 = li0; out.focus = foc; out.panel = { w: PW, h: PH };
      // the carousel measures about 3000px across five panels and 1640 across three at 1:1 — the board's fit for the room
      const five = (W - 60) / 3000, three = (W - 40) / 1640; out.fitZ = +Math.min(1, five >= 0.8 ? five : Math.max(0.45, three)).toFixed(3);
      out.station = { mid: t.mid, who: t.who, t: t.t, text: t.text };
      return out;
    }
    // ISO — the board's exploded scene (Canvas.dc.html): u = STATION, v = the entity axis, z = the floor. Every layer is
    // its own plate in a row along u (context graph · read · the exchange · produced · session canvas, then the two
    // optional layers as stations of their own), identical parallelograms VSPAN deep; a turn is a floor, several turns
    // floors one above the other. An item stands on its stem at its pin, centred in the span, a column to the right
    // past six; the context records scatter about the PROMPT LINE, nearer it the more relevant, a lane per family
    // along the plate. A run is the board's ISO.route: every leg changes exactly one of u, v or z — out of the pin,
    // along u to the gap between the two stations, along v, along u into the target's pin; a run bound backwards, or
    // past a station, goes round the far end. Runs sharing a pin leave and arrive by their own PORTS, runs sharing a
    // gap by their own LANE, so no two legs of different runs ever share a length.
    // the board's single-turn iso (`turn` beside `stacked`): only the selected turn's floor; otherwise every turn, a floor each (Stack overrides Turn)
    const SOLO = !!o.solo && !o.stack && turns.length > 1; if (SOLO) { turns = [turns[sel]]; sel = 0; }
    const STK = turns.length > 1;
    const TILT = o.tilt == null ? (STK ? 12 : 30) : Math.max(12, Math.min(60, +o.tilt)), AZIM = o.azim == null ? 45 : Math.max(25, Math.min(65, +o.azim));   // a stack starts flatter, so its floors read as floors (the board)
    const P = o.proj || isoP(TILT, AZIM);   // the classic isometric the whole design draws with, unless the view was tilted or swung
    const WSZ = o.wsz === 's' || o.wsz === 'l' ? o.wsz : 'm';
    const ACT = !!(o.layers && o.layers.activity), EST = !!(o.layers && o.layers.estate), GAL = o.galaxy !== false;
    out.solo = SOLO; out.tilt = TILT; out.azim = AZIM; out.anodes = []; out.enodes = []; out.bands = []; out.widgets = []; out.stack = STK; out.wsz = WSZ;
    const KI = STK ? 340 : 300, UXS = 1.5, VSPAN = STK ? 5 : den === 'full' ? 6 : 5;   // the board's ground: 300 px a unit, the stations 1.5 units apart   // px per ground unit; the u axis stretched so neighbouring cards never meet; the plates' depth
    const CW = STK ? 172 : 206, CH = STK ? 47 : 58, RAISE = STK ? 10 : 14, RPC = 6, DU = 1.34, MAXC = 4, CAP = RPC * MAXC, CAPG = 12;
    const FW = { s: 60, m: 78, l: 108 }[WSZ], FH = Math.round(FW * 0.56);
    const isWidget = isWidgetCard;
    const GU = UXS * KI, GV = KI;   // a station unit and an entity unit, in ground px
    const proj = (U, V, z) => { const p = P(U, V, z || 0); return { x: p[0], y: p[1] }; };
    const ESTL = { key: 'estate', name: 'estate', sub: 'where it ran', col: 'var(--xp-dv3)', kind: 'nodes' }, ACTL = { key: 'activity', name: 'activity', sub: 'what ran', col: 'var(--xp-ac2)', kind: 'acts' };
    const STA = LAYERS.concat(ACT ? [ACTL] : [], EST ? [ESTL] : []);
    const graphs = turns.map(gData), actsL = turns.map((t) => ACT ? actsOf(t) : []), estsL = turns.map((t) => EST ? estOf(t) : []);
    const colsOf = (n) => Math.max(1, Math.min(MAXC, Math.ceil(n / RPC)));
    // every station is as wide as its fullest turn needs (a column per six items past the first), so the stations line up
    // across the floors; the context plate is the wide one (relevance spreads across it)
    const colsAt = STA.map((L) => Math.max.apply(null, turns.map((t, si) => L.kind === 'graph' ? 1 : L.kind === 'acts' ? Math.max(1, Math.min(MAXC, Math.ceil(actsL[si].length / 8))) : L.kind === 'nodes' ? colsOf(estsL[si].length) : colsOf(Math.min(CAP, cardsOf(t, L.key).length))).concat([1])));
    const halfW = (i) => i === 0 ? 0.64 : 0.44, GAP = 0.12;
    const uOrg = []; { let u = 0; STA.forEach((L, i) => { uOrg[i] = u; u += 2 * halfW(i) + (colsAt[i] - 1) * DU + GAP; }); }
    const uC = (i, cj) => uOrg[i] + halfW(i) + (cj || 0) * DU;                       // a column's centre line
    const uA = (i) => uOrg[i], uB = (i) => uOrg[i] + 2 * halfW(i) + (colsAt[i] - 1) * DU;   // a plate's u extent
    const vA = -0.62, vB = VSPAN + 0.62, GALV = GAL ? 1.7 : 0;                          // the plates' v extent; the galaxy sheet lies on the floor past the graph plate
    const fU0 = uA(0) - 0.12, fU1 = uB(STA.length - 1) + 0.12, fV0 = vA - 0.16, fV1 = vB + 0.16 + GALV;   // the floor
    // a floor is held clear of the one beneath: its projected height plus a gap, in z (the board's derived separation)
    const ZH = (() => { const c = [proj(fU0 * GU, fV0 * GV, 0), proj(fU1 * GU, fV0 * GV, 0), proj(fU1 * GU, fV1 * GV, 0), proj(fU0 * GU, fV1 * GV, 0)]; const ys = c.map((q) => q.y); const perZ = Math.max(0.05, (proj(0, 0, 0).y - proj(0, 0, 100).y) / 100); return Math.round((Math.max.apply(null, ys) - Math.min.apply(null, ys) + CH + 60) / perZ); })();
    const pts = []; const pinsG = {};
    const pin = (U, V, z, r) => ({ U, V, z: z || 0, r: r || 0 });
    turns.forEach((t, si) => {
      const z = STK ? (turns.length - 1 - si) * ZH : 0, G = pinsG[t.mid] = {}; const g = graphs[si];
      const fc = [proj(fU0 * GU, fV0 * GV, z), proj(fU1 * GU, fV0 * GV, z), proj(fU1 * GU, fV1 * GV, z), proj(fU0 * GU, fV1 * GV, z)]; fc.forEach((c) => pts.push(c));
      out.plates.push({ si, mid: t.mid, cls: si === sel ? 'on' : '', poly: fc.map((c) => ({ x: c.x, y: c.y })), z, u0: fU0 * GU, pw: (fU1 - fU0) * GU, ph: (fV1 - fV0) * GV });
      const lb = proj((uA(0) + 0.04) * GU, (vA - 0.5) * GV, z); out.labels.push({ si, x: lb.x, y: lb.y, n: (t.who || 'you') + ' · ' + (t.t || ''), k: String(t.text || '').slice(0, 40), cls: 'station' + (si === sel ? ' on' : ''), col: 'var(--xp-t2)', mid: t.mid });
      STA.forEach((L, i) => {
        const U0 = uA(i) * GU, U1 = uB(i) * GU, V0 = vA * GV, V1 = vB * GV;
        const bc = [proj(U0, V0, z), proj(U1, V0, z), proj(U1, V1, z), proj(U0, V1, z)]; bc.forEach((c) => pts.push(c));
        const list = L.kind ? [] : cardsOf(t, L.key), acts = actsL[si], ests = estsL[si];
        const empty = L.kind === 'graph' ? !g.nodes.length : L.kind === 'acts' ? !acts.length : L.kind === 'nodes' ? !ests.length : !list.length;
        out.bands.push({ si, mid: t.mid, layer: L.key, poly: bc.map((c) => ({ x: c.x, y: c.y })), col: L.col, empty, cls: (si === sel ? 'on' : '') + (L.key === 'say' && pendOf(t) ? ' gen' : ''), u0: U0, pw: U1 - U0, v0: V0, vb: V1 - V0 });
        const ll = proj((uA(i) + 0.04) * GU, (vA - 0.3) * GV, z);
        const count = L.kind === 'graph' ? g.nodes.length : L.kind === 'acts' ? acts.length : L.kind === 'nodes' ? ests.length : list.length;
        out.labels.push({ si, x: ll.x, y: ll.y, n: L.name, k: L.kind === 'acts' ? count + ' call' + (count === 1 ? '' : 's') : String(count), cls: 'layer sm ' + L.key, col: L.col, st: i });
        if (empty) { const e = proj((U0 + U1) / 2, (V0 + V1) / 2, z); out.labels.push({ si, x: e.x, y: e.y, n: L.key === 'say' ? 'no reply yet' : L.key === 'read' ? 'nothing read' : L.key === 'made' ? 'nothing produced' : L.key === 'land' ? 'nothing landed' : L.key === 'graph' ? 'nothing read' : L.key === 'activity' ? 'no calls' : 'nowhere yet', k: '', cls: 'layer sm empty', col: 'var(--xp-t3)' }); return; }
        if (L.kind === 'graph') {
          // the PROMPT LINE down the plate's middle: a record sits nearer it the more relevant it is (alternate sides), a
          // lane per family along the plate; the twelve most relevant on the plate, the rest a count
          const uc = uC(0) * GU; const a0 = proj(uc, -0.5 * GV, z), a1 = proj(uc, (VSPAN + 0.5) * GV, z);
          out.edges.push({ x: px(a0.x), y: px(a0.y), len: px(Math.hypot(a1.x - a0.x, a1.y - a0.y)), deg: +(Math.atan2(a1.y - a0.y, a1.x - a0.x) * 180 / Math.PI).toFixed(2), col: 'var(--xp-dv1)', cls: 'thin prompt', title: 'the prompt — nearer the line, more relevant', raw: true });
          out.labels.push({ si, x: a0.x, y: a0.y - 14, n: 'the prompt', k: g.nodes.length + ' rec · nearer the line, more relevant', cls: 'layer sm prompt', col: 'var(--xp-dv1)' });
          const shown = g.nodes.slice().sort((a, b) => b.score - a.score).slice(0, CAPG), more = g.nodes.length - shown.length;
          const lanes = LANES.filter((l) => shown.some((n) => n.lane === l)), nsec = lanes.length;
          const vSec = (k) => nsec > 1 ? 0.5 + k * (VSPAN - 1) / (nsec - 1) : VSPAN / 2;
          lanes.forEach((lane, k) => { const M = shown.filter((n) => n.lane === lane).sort((a, b) => b.score - a.score), n = M.length; const lp = proj((uA(0) - 0.46) * GU, vSec(k) * GV, z);
            out.labels.push({ si, x: lp.x, y: lp.y - 7, n: lane, k: String(g.nodes.filter((x) => x.lane === lane).length), cls: 'layer sm lane', col: M[0].col || L.col });
            M.forEach((nd, mi) => { const rel = Math.max(0, Math.min(1, nd.score)); const gi = shown.indexOf(nd); const du = (Math.min(0.56, (1 - rel) * 1.5) + gi * 0.012) * (mi % 2 ? 1 : -1), dv = n > 1 ? (mi - (n - 1) / 2) * Math.min(0.36, 1.1 / (n - 1)) : 0;
              const U = (uC(0) + du) * GU, V = (vSec(k) + dv) * GV, p = proj(U, V, z); pts.push(p); const d = Math.round(19 + rel * 9); G['node:' + nd.id] = pin(U, V, z, d / 2);
              out.gnodes.push({ id: t.mid + ':graph:' + nd.id, nid: nd.id, mid: t.mid, si, x: p.x, y: p.y, d, col: nd.col || L.col, icon: ICON_OF(nd.kind, nd.lane), label: nd.label, lane: nd.lane, kind: nd.kind, score: rel, ghost: nd.included === false, lit: rel > 0.82, op: +(0.45 + rel * 0.55).toFixed(2) }); }); });
          if (more > 0) { const mp = proj(U1 - 20, V1 - 30, z); out.labels.push({ si, x: mp.x, y: mp.y, n: '+' + more + ' more', k: 'in the context', cls: 'layer sm more', col: L.col, mid: t.mid }); }
          // the context galaxy, an iso sheet lying on the floor past the plate — an option (the Context menu's own widget)
          if (GAL) { const GW = STK ? 220 : 280; const c = proj(uc, (vB + GALV / 2) * GV, z); pts.push(c); out.graphs.push({ id: t.mid + ':graph', mid: t.mid, si, x: c.x, y: c.y, w: GW, h: galH(GW), data: g, iso: true }); }
          return; }
        if (L.kind === 'acts') {
          // the ACTIVITY as a graph of what ran: the calls down the plate in time order (eight to a column), a child a step
          // to the right of its parent — the tree reads across the plate, the sequence down it
          const tree = actTree(acts, 8); const N = acts.length, per = 8, pitch = N > 1 ? Math.min(0.9, (VSPAN - 0.6) / (Math.min(per, N) - 1)) : 0;
          acts.forEach((a, ai) => { const col = Math.floor(ai / per), row = ai % per; const rowsIn = Math.min(per, N - col * per); const U = (uC(i, col) - 0.22 + Math.min(2, tree.depth[ai]) * 0.22) * GU, V = (row * pitch + (VSPAN - (rowsIn - 1) * pitch) / 2) * GV; const p = proj(U, V, z); pts.push(p); G['act:' + ai] = pin(U, V, z, 11);
            out.anodes.push({ id: t.mid + ':act:' + ai, mid: t.mid, si, x: p.x, y: p.y, d: 22, col: actCol(a), icon: ICON.cap, label: a.n || a.cap || 'call', meta: actMeta(a), status: String(a.status || ''), lane: 'activity', depth: tree.depth[ai] }); });
          return; }
        if (L.kind === 'nodes') {
          const N = ests.length, per = RPC, pitch = N > 1 ? Math.min(1, (VSPAN - 0.6) / (Math.min(per, N) - 1)) : 0;
          ests.forEach((e, ei) => { const col = Math.floor(ei / per), row = ei % per; const rowsIn = Math.min(per, N - col * per); const U = uC(i, col) * GU, V = (row * pitch + (VSPAN - (rowsIn - 1) * pitch) / 2) * GV; const p = proj(U, V, z); pts.push(p); G['est:' + ei] = pin(U, V, z, 12);
            out.enodes.push({ id: t.mid + ':est:' + ei, mid: t.mid, si, x: p.x, y: p.y, d: 24, col: estCol(e), icon: ICON_OF(e.kind, 'estate'), label: e.label || e.id, meta: e.detail || e.kind || '', status: String(e.status || ''), lane: 'estate' }); });
          return; }
        // the items: down the rows first (centred in the span), then a column to the right; a canvas item dropped
        // somewhere on the plate (card.at.iso, ground px relative to the plate) stands there
        const shown = list.slice(0, CAP), more = list.length - shown.length, cols = colsOf(shown.length), RPB = Math.ceil(shown.length / cols); let flow = 0;
        if (more > 0) { const mp = proj(U1 - 20, V1 - 30, z); out.labels.push({ si, x: mp.x, y: mp.y, n: '+' + more + ' more', k: 'in the ' + L.name, cls: 'layer sm more', col: L.col, mid: t.mid }); }
        shown.forEach((c, ci) => { const at = L.key === 'land' && c.at && c.at.iso; let U, V;
          if (at && isFinite(+at.u)) { U = U0 + Math.max(40, Math.min(U1 - U0 - 40, +at.u)); V = V0 + Math.max(60, Math.min(V1 - V0 - 40, +at.v)); }
          else { const cj = Math.floor(flow / RPB), rj = flow % RPB, rowsIn = Math.min(RPB, shown.length - cj * RPB), pitch = RPB > VSPAN + 1 ? VSPAN / (RPB - 1) : 1; flow++; U = uC(i, cj) * GU; V = (rj * pitch + (VSPAN - (rowsIn - 1) * pitch) / 2) * GV; }
          const p = proj(U, V, z); pts.push(p); const wd = widgetOf(c), id = t.mid + ':' + L.key + ':' + ci, grp = isWidget(c); G[L.key + ':' + ci] = pin(U, V, z);
          out.widgets.push({ id, mid: t.mid, si, layer: L.key, ci, x: p.x, y: p.y, w: FW, h: FH, cw: CW, ch: CH, stem: RAISE, card: c, col: c.col || L.col, form: wd.form, data: wd.data, placed: !!c.tpl, sample: !!wd.sample, draw: grp ? 'group' : 'card', value: grp ? valueOf(wd) : '', tight: STK, drag: L.key === 'land' && grp, key: c.key || '' });
          out.cards.push({ id, mid: t.mid, si, layer: L.key, ci, x: p.x, y: p.y, w: CW, h: CH, card: c, col: c.col || L.col, anchored: true, iw: true }); });
      });
    });
    // fit the scene into the frame: scale and shift. The items counter-scale by the FIT alone (1:1 text when fitted,
    // capped as the board's embed caps it) — the pan zoom then grows and shrinks them with the scene
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity; pts.forEach((p) => { x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y); x1 = Math.max(x1, p.x); y1 = Math.max(y1, p.y); });
    const s = Math.max(0.2, Math.min(1.4, Math.min((W - 80) / Math.max(1, x1 - x0 + 200), (H - 120) / Math.max(1, y1 - y0 + 140))));   // a busy turn is wide (a column per six items, seven stations): it still fits, down to a fifth
    const dx = W / 2 - s * (x0 + x1) / 2, dy = H / 2 - s * (y0 + y1) / 2 + 20;
    out.fit = { s: +s.toFixed(3), x: px(dx), y: px(dy) }; out.ground = { s, dx, dy, tilt: TILT, azim: AZIM, custom: !!o.proj };   // the drop's way back from a screen point to the ground
    out.inv = +(s * Math.min(1.2, 1 / Math.min(1, s))).toFixed(3);   /* the items follow the fit, grown no more than 1.2x against a small scene: a station's pitch always clears a card */
    const T = (p) => ({ x: px(p.x * s + dx), y: px(p.y * s + dy) });
    out.plates.forEach((pl) => { pl.poly = pl.poly.map(T); });
    out.bands.forEach((b) => { b.poly = b.poly.map(T); });
    const edgesOf = (poly) => poly.map((a, i) => { const b = poly[(i + 1) % poly.length]; return { x: a.x, y: a.y, len: px(Math.hypot(b.x - a.x, b.y - a.y)), deg: +(Math.atan2(b.y - a.y, b.x - a.x) * 180 / Math.PI).toFixed(2) }; });
    out.outline = []; out.plates.forEach((pl) => { edgesOf(pl.poly).forEach((e) => out.outline.push(Object.assign(e, { si: pl.si, cls: pl.cls }))); });
    out.boutline = []; out.bands.forEach((b) => { edgesOf(b.poly).forEach((e) => out.boutline.push(Object.assign(e, { si: b.si, col: b.col }))); });
    out.labels.forEach((l) => { const q = T(l); l.x = q.x; l.y = q.y; });
    out.cards.forEach((c) => { const q = T(c); c.x = q.x; c.y = q.y; });
    out.widgets.forEach((c) => { const q = T(c); c.x = q.x; c.y = q.y; });
    out.gnodes.forEach((n) => { const q = T(n); n.x = q.x; n.y = q.y; });
    out.anodes.forEach((n) => { const q = T(n); n.x = q.x; n.y = q.y; });
    out.enodes.forEach((n) => { const q = T(n); n.x = q.x; n.y = q.y; });
    out.graphs.forEach((g) => { const q = T(g); g.x = q.x; g.y = q.y; });
    const kept = out.edges.filter((e) => e.raw).map((e) => { const a = T({ x: e.x, y: e.y }); return Object.assign({}, e, { x: a.x, y: a.y, len: px(e.len * s), raw: undefined }); });
    out.edges = kept; out.runs = 0;
    // ── the runs: the board's ISO.route on the ground, projected through P and the fit ────────────────────────────
    const at = (U, V, z) => { const p = proj(U, V, z); return { x: p.x * s + dx, y: p.y * s + dy }; };   // unrounded: a short leg keeps its exact direction
    const stationOf = (U) => { let best = 0; STA.forEach((L, i) => { if (U >= uA(i) * GU - 1) best = i; }); return best; };
    const LANE = 18, PORT = 12, STEP = 27, VO = (VSPAN + 0.9) * GV;   // a lane's pitch, a port's pitch, and a step aside that never lands on a port line (27 is no multiple of 6)   // a lane's pitch in the gap, a port's pitch at a pin, the far end a round-about run travels past
    let rid = 0;
    turns.forEach((t, si) => { const G = pinsG[t.mid] || {}; const gp = (k) => G[k] || null; const runs = [];
      const add = (A, B, col, cls, title, joins, opt) => { if (!A || !B) return; runs.push(Object.assign({ A, B, col, cls, title, joins, id: ++rid }, opt || {})); };
      const np = (c) => gp('node:' + String((c && (c.id != null ? c.id : c.n)) || '')), g = graphs[si];
      const reads = cardsOf(t, 'read').slice(0, CAP), mades = cardsOf(t, 'made').slice(0, CAP), lands = cardsOf(t, 'land').slice(0, CAP);
      reads.forEach((c, j) => { const n = np(c); if (!n || !gp('read:' + j)) return; const gn = g.nodes.find((x) => x.id === String((c.id != null ? c.id : c.n) || '')), mem = !!(gn && gn.lane === 'memory'); add(n, gp('read:' + j), mem ? 'var(--xp-ac2)' : 'var(--xp-dv1)', mem ? 'in mem' : 'in', mem ? 'the memory that was recalled' : 'the context entry that was injected'); });
      const ex = gp('say:0');
      if (ex) { reads.forEach((c, j) => add(gp('read:' + j), ex, 'var(--xp-dv1)', 'in', 'the passage the answer is based on')); if (!reads.length && g.nodes.length) { const first = g.nodes.slice().sort((a, b) => b.score - a.score)[0]; add(gp('node:' + first.id), ex, 'var(--xp-dv1)', 'in', 'the context this turn read'); } mades.forEach((c, j) => add(ex, gp('made:' + j), 'var(--xp-dv2)', 'out', 'produced by this turn')); }
      landRuns(mades, lands, (mi, li, why) => add(mi == null ? ex : gp('made:' + mi), gp('land:' + li), 'var(--xp-ac)', mi == null ? 'link dash' : 'link', why));
      g.nodes.filter((n) => n.lane === 'canvas').forEach((n) => { const li = lands.findIndex((c) => String(c.id != null ? c.id : c.n) === n.id); if (li >= 0 && gp('land:' + li) && gp('node:' + n.id)) add(gp('land:' + li), gp('node:' + n.id), 'var(--xp-ac2)', 'pin', 'pinned back into the next prompt'); });
      // the activity: a graph of what ran — parent to child, step to step; the exchange (or the card that ran it) joins the FIRST root only
      const acts = actsL[si]; if (acts.length) { const tree = actTree(acts, 8); let joined = false;
        actRuns(acts, mades, (from, ai, cls, title) => { if (cls === 'rel step act') { add(gp('act:' + from[1]), gp('act:' + ai), 'var(--xp-ac2)', cls, title, null, { direct: true }); return; }
          if (from === 'ex' || from[0] === 'made') { if (from[0] === 'made') { add(gp('made:' + from[1]), gp('act:' + ai), 'var(--xp-ac2)', cls, title); return; } if (joined) return; joined = true; add(ex, gp('act:' + ai), 'var(--xp-ac2)', cls, 'the calls this turn made — the first of them'); return; }
          add(gp('act:' + from[1]), gp('act:' + ai), 'var(--xp-ac2)', cls, title); }); void tree; }
      estsL[si].forEach((e, ei) => { estRuns(e, (ai) => gp('act:' + ai)).forEach(([ai, title]) => add(gp('act:' + ai), gp('est:' + ei), 'var(--xp-dv3)', 'est', title)); if (e.via != null && gp('est:' + e.via)) add(gp('est:' + e.via), gp('est:' + ei), 'var(--xp-dv3)', 'rel est dash', (e.label || e.id) + ' serves it', null, { direct: true }); });
      g.rels.forEach((r) => { const a = gp('node:' + r.from), b = gp('node:' + r.to), Rc = RELC[r.kind] || RELC.cite; if (a && b) add(a, b, Rc[0], Rc[1], Rc[2], [String(r.from), String(r.to)], { rel: true }); });
      // PORTS: the runs at one pin, fanned in the order of their far ends; LANES: the runs through one gap, fanned in the
      // order of their targets — so neighbouring runs stay parallel and never have to cross to reach neighbouring pins
      const byPin = {}; runs.forEach((r) => { (byPin[r.A.U + '/' + r.A.V] = byPin[r.A.U + '/' + r.A.V] || []).push([r, 'a']); (byPin[r.B.U + '/' + r.B.V] = byPin[r.B.U + '/' + r.B.V] || []).push([r, 'b']); });
      Object.keys(byPin).forEach((k) => { const Q = byPin[k]; Q.sort((p, q) => { const fa = p[1] === 'a' ? p[0].B : p[0].A, fb = q[1] === 'a' ? q[0].B : q[0].A; return (fa.V - fb.V) || (fa.U - fb.U); }); const n = Q.length; Q.forEach(([r, end], j) => { r[end === 'a' ? 'pa' : 'pb'] = (j - (n - 1) / 2) * PORT; r[end === 'a' ? 'fa' : 'fb'] = (n - 1) / 2 * PORT; }); });   /* and how wide the fan is: a step aside starts past it */
      const byGap = {}; runs.forEach((r) => { const sa = stationOf(r.A.U), sb = stationOf(r.B.U); r.sa = sa; r.sb = sb; r.same = sa === sb; r.around = sb < sa || sb - sa > 1; const key = r.same ? 's' + sa : r.around ? 'o' : 'g' + Math.min(sa, sb); (byGap[key] = byGap[key] || []).push(r); });
      Object.keys(byGap).forEach((k) => { const Q = byGap[k]; Q.sort((p, q) => (p.B.V - q.B.V) || (p.A.V - q.A.V)); const n = Q.length; Q.forEach((r, j) => { r.off = (k === 'o' || k[0] === 's') ? j * LANE : (j - (n - 1) / 2) * LANE; }); });   /* a round-about run and a run within one station take their own lane outward, never a mirrored one */
      runs.forEach((r) => { const A = r.A, B = r.B, pa = r.pa || 0, pb = r.pb || 0, off = r.off || 0; let W_;
        if (r.direct && Math.abs(A.U - B.U) < 0.5) W_ = [[A.U, A.V], [B.U, B.V]];   // a step to the next call in the column: one leg along v
        else if (r.around) { const vo = VO + off, ouA = (r.fa || 0) + STEP + off, ouB = (r.fb || 0) + STEP + off; W_ = [[A.U + pa, A.V], [A.U + pa, A.V + pa], [A.U + pa + ouA, A.V + pa], [A.U + pa + ouA, vo], [B.U + pb + ouB, vo], [B.U + pb + ouB, B.V + pb], [B.U + pb, B.V + pb], [B.U + pb, B.V]]; }   // round the far end: along v past the plates, along u, back along v
        else { let um; if (r.same) { const lo = uA(r.sa) * GU + 8, hi = uB(r.sa) * GU - 8;   // within one station: between the two (the board), or beside them when they stand in one column — inside the plate, never in a gap's lanes
            const st = Math.max(r.fa || 0, r.fb || 0) + STEP; um = Math.abs(A.U - B.U) >= 2 * st ? (A.U + B.U) / 2 + off - 15 : Math.max(A.U, B.U) + st + off; if (um > hi) um = Math.min(A.U, B.U) - st - off; um = Math.max(lo, Math.min(hi, um)); }
          else um = (uB(r.sa) * GU + uA(r.sb) * GU) / 2 + off;   // the gap between the two stations
          W_ = [[A.U + pa, A.V], [A.U + pa, A.V + pa], [um, A.V + pa], [um, B.V + pb], [B.U + pb, B.V + pb], [B.U + pb, B.V]]; }
        const Pp = W_.map((q) => at(q[0], q[1], A.z)); out.runs++;
        // the ends: a node is met at its rim, not its centre (the last leg shortened along itself)
        const trim = (p, q, rr) => { const d = Math.hypot(q.x - p.x, q.y - p.y); if (!rr || d < 1) return p; const k = Math.min(0.9, rr * out.inv / d); return { x: p.x + (q.x - p.x) * k, y: p.y + (q.y - p.y) * k }; };
        Pp[0] = trim(Pp[0], Pp[1], A.r); Pp[Pp.length - 1] = trim(Pp[Pp.length - 1], Pp[Pp.length - 2], B.r);
        for (let n = 0; n < Pp.length - 1; n++) { const p = Pp[n], q = Pp[n + 1]; const dxx = q.x - p.x, dyy = q.y - p.y, LN = Math.hypot(dxx, dyy); if (LN < 2.5) continue;
          out.edges.push({ x: px(p.x), y: px(p.y), len: px(LN), deg: +(Math.atan2(dyy, dxx) * 180 / Math.PI).toFixed(2), col: r.col, cls: r.cls, title: r.title, run: r.id, joins: r.joins || undefined }); } }); });
    out.size = { w: W, h: H };
    return out;
  }
  /* ── the turn's ACTIVITY and ESTATE (the two optional layers), as the host feeds them ──────────────────────────
     turn.activity = [{n|cap, ts, ms, status: ok|error|run, group, parent?}]  — the capability calls the turn made, in order
     turn.estate   = [{id, label, kind: host|container|service|category, status, detail, acts:[i…], via?: j}]
                     — the subsystems the calls ran through and the machines behind them (via: the index it serves) */
  const actsOf = (t) => Array.isArray(t && t.activity) ? t.activity.slice(0, 24) : [];
  const estOf = (t) => Array.isArray(t && t.estate) ? t.estate.slice(0, 12) : [];
  const actCol = (a) => { const s = String((a && a.status) || '').toLowerCase(); return /err|fail/.test(s) ? 'var(--xp-red)' : /run|call|start/.test(s) ? 'var(--xp-ac)' : 'var(--xp-ac2)'; };
  const actMeta = (a) => [a.t || '', a.ms != null ? a.ms + ' ms' : '', String(a.status || '')].filter(Boolean).join(' · ');
  const estCol = (e) => { const s = String((e && e.status) || '').toLowerCase(); return s === 'err' || s === 'error' ? 'var(--xp-red)' : s === 'warn' ? 'var(--xp-dv2)' : s === 'ok' ? 'var(--xp-ac2)' : 'var(--xp-dv3)'; };
  // the card that triggered a call: a produced capability card whose name starts with the cap's name (the chat's
  // capability cards are titled by the cap), else the exchange (null)
  const actParent = (a, mades) => { if (a && a.parent != null && mades[a.parent]) return a.parent; const cap = String((a && (a.cap || a.n)) || '').toLowerCase().split(/\s/)[0]; if (!cap) return null;
    const i = mades.findIndex((m) => /^(cap|capability|tool)$/i.test(String(m.kind || '')) && String(m.n || '').toLowerCase().indexOf(cap) === 0); return i >= 0 ? i : null; };
  // every canvas item is joined to something: the product it came from — the same key, else the same place in the
  // list — or, hand-placed (nothing produced it), the exchange it was placed on. cb(madeIndex|null, landIndex, why)
  function landRuns(mades, lands, cb) {
    lands.forEach((l, j) => { let mi = -1; if (l && l.key) mi = mades.findIndex((m) => m && m.key && m.key === l.key);
      if (mi < 0 && mades[j] && !(mades[j].key && l && l.key && mades[j].key !== l.key)) mi = j;
      if (mi >= 0) cb(mi, j, 'this became a canvas item'); else cb(null, j, l && l.tpl ? 'placed on this turn from the registry' : 'placed on this turn'); });
  }
  /* the activity as a TREE: a call's parent is the call that triggered it (the host resolves the event's trigger id to an
     index, `parent`), else the capability card that names it (`card`), else the exchange. rows[] lists the indices per
     depth in time order, so the tree lays out top-down; runs go parent → child, and the siblings of one parent are
     chained in time order (a thin step relation) — the structure of what ran, not a fan back to the chat item. */
  function actTree(acts, per) { const N = (acts || []).length, parent = [], depth = [], rows = [];
    for (let i = 0; i < N; i++) { const p = acts[i] && acts[i].parent; parent[i] = (p != null && p !== i && p >= 0 && p < N) ? +p : -1; }
    const dOf = (i, seen) => { if (parent[i] < 0) return 0; if (seen[i]) return 0; seen[i] = 1; return 1 + dOf(parent[i], seen); };
    for (let i = 0; i < N; i++) { depth[i] = Math.min(6, dOf(i, {})); (rows[depth[i]] = rows[depth[i]] || []).push(i); }
    const W = Math.max(1, per || 6), wrapped = []; rows.filter(Boolean).forEach((row) => { for (let i = 0; i < row.length; i += W) wrapped.push(row.slice(i, i + W)); });   /* a row wraps: a busy turn stays on its plate */
    return { parent, depth, rows: wrapped }; }
  /* the calls a source triggered leave it by ONE departure (the exchange to its roots, a card to the calls it ran, a parent
     to its children) and branch at their own ports; a step between siblings is its own short run */
  function actTrunk(from, cls) { return cls === 'rel step act' ? null : { trunk: 'act:' + (from === 'ex' ? 'ex' : from[0] + ':' + from[1]), tend: 'a' }; }
  /* the estate layer says WHERE the turn ran: a few calls through a subsystem are joined each; many are one counted run */
  function estRuns(e, has) { const A = (e.acts || []).filter((ai) => has(ai)), name = e.label || e.id; if (A.length > 3) return [[A[0], A.length + ' calls ran through ' + name]]; return A.map((ai) => [ai, 'ran through ' + name]); }
  function actRuns(acts, mades, cb) { const T = actTree(acts), last = {};
    acts.forEach((a, ai) => { const p = T.parent[ai];
      if (p >= 0) cb(['act', p], ai, 'act', 'triggered by ' + (acts[p].n || acts[p].cap || 'the call above') + (a.ms != null ? ' · ' + a.ms + ' ms' : ''));
      else { const src = actParent(a, mades); cb(src != null ? ['made', src] : 'ex', ai, 'act', (src != null ? 'this capability call, run by the card' : 'a call this turn made') + (a.ms != null ? ' · ' + a.ms + ' ms' : '')); }
      const k = String(p); if (last[k] != null) cb(['act', last[k]], ai, 'rel step act', 'the next call'); last[k] = ai; }); }
  const isWidgetCard = (c) => !!(c && (c.tpl || c.form || (c.record && typeof c.record === 'object') || String(c.kind || '').toLowerCase() === 'widget'));

  /* ── the CARDS router: every run collected, then laned ──────────────────────────────────────────────────────────
     A run leaves a card by its side, travels the gutter between the stations (or, two stations apart, drops to a bus
     lane under the row), and comes level into its target. Lanes are allotted per gutter in the order the runs are
     GOING — a descending run bound lower takes the outer lane, an ascending one bound higher the same — so bundles
     come out parallel; ports fan a card's side in lane order so no two legs ever share a length. Pure. */
  function cardsRouter(G) {
    const runs = [];
    const add = (A, B, col, cls, title, joins, o) => { if (A && B) runs.push(Object.assign({ A, B, col, cls: cls || '', title: title || '', joins }, o || {})); };
    const gx0 = (k) => G.xU(k) - 16 + G.SWD + G.SGP / 2;                     // the centre of the gutter right of station k
    const px0 = (k) => G.xU(k) + G.SWD - 44;                                  // the in-plate lane, in the plate's right margin
    const flush = () => {
      const groups = {};   // gutter key -> [{r, ty, desc}]
      const want = (k, r, ty, desc) => { (groups[k] = groups[k] || []).push({ r, ty, desc }); };
      // a TRUNK: the runs of one kind into one target (or out of one source) in one gutter share a lane, and the shared
      // end is drawn once — the board's "five or six arrivals, not seventeen". The first member carries the lane; the
      // rest borrow it and skip their own shared end.
      const trunks = {};
      runs.forEach((r) => { const d = r.B.st - r.A.st; r.dir = d > 0 ? 1 : d < 0 ? -1 : 0; r.same = d === 0; r.bus = Math.abs(d) >= 2;
        if (r.trunk && !r.same && !r.bus) { const k = r.trunk + '|' + (r.tend === 'a' ? key(r.A) : key(r.B)); if (trunks[k]) { r.lead = trunks[k]; (r.lead.members = r.lead.members || []).push(r); return; } trunks[k] = r; }
        if (r.same) { if (Math.abs(r.A.cy - r.B.cy) < 1 || Math.abs(r.A.cx - r.B.cx) < 1) return; want('p' + r.A.st, r, r.B.cy, r.B.cy > r.A.cy); }
        else if (!r.bus) want('g' + Math.min(r.A.st, r.B.st), r, r.B.cy, r.B.cy > r.A.cy);
        else { r.gA = 'g' + (r.dir > 0 ? r.A.st : r.A.st - 1); r.gB = 'g' + (r.dir > 0 ? r.B.st - 1 : r.B.st); want(r.gA, r, 1e9, true); want(r.gB, r, r.B.cy, false); } });
      // the bus lanes under the row: one per far run
      let nb = 0; runs.forEach((r) => { if (r.bus) { r.yl = G.rowBottom + 14 + (nb++) * 7; } });
      Object.keys(groups).forEach((k) => { const L = groups[k];
        // the order: descending runs first, bound lower first; then ascending, bound higher first — that is left to right
        const desc = L.filter((e) => e.desc).sort((a, b) => b.ty - a.ty), asc = L.filter((e) => !e.desc).sort((a, b) => a.ty - b.ty); const all = desc.concat(asc), n = all.length;
        const inPlate = k[0] === 'p', room = inPlate ? 40 : Math.max(8, G.SGP - 10), pitch = Math.min(inPlate ? 6 : 9, room / Math.max(1, n - 1));
        const x0 = inPlate ? px0(+k.slice(1)) : gx0(+k.slice(1));
        all.forEach((e, i) => { const x = x0 + (i - (n - 1) / 2) * pitch; if (e.r.bus) { if (k === e.r.gA) e.r.lxA = x; else e.r.lxB = x; } else e.r.lx = x; }); });
      // ports: a card's side fans its departures and arrivals in lane order, so a bundle leaves and lands parallel.
      // A NODE (a context record, a call, an estate node — small, several to a row) is joined from above or below
      // instead: a short plumb leg to a band between the rows, then level to the lane. The band is shared by the row
      // and fanned by reach — the node furthest from the lane takes the outer band — so two nodes in one row never
      // share a horizontal and a stub never crosses a neighbour's run.
      const ports = {}, nports = {};
      const side = (box, x) => (x > box.cx ? 'R' : 'L');
      runs.forEach((r) => { if (r.lead) { r.lx = r.lead.lx; r.dir = r.lead.dir; } if (r.same && r.lx == null) return;
        const xa = r.bus ? r.lxA : r.lx, xb = r.bus ? r.lxB : r.lx;
        r.sa = r.same ? 'R' : side(r.A, xa); r.sb = r.same ? 'R' : side(r.B, xb);
        [['a', r.A, xa, r.B], ['b', r.B, xb, r.A]].forEach((e) => { if (r.lead && e[0] === (r.tend || 'b')) return;   // a member's shared end is the lead's
          const box = e[1], other = e[3];
          if (box.node) { let vs = other.cy > box.cy + 1 ? 'B' : other.cy < box.cy - 1 ? 'T' : 'B';
            const plumb = (b, sd) => runs.some((q) => q !== r && q.same && Math.abs(q.A.cx - q.B.cx) < 1 && ((q.A === b && (sd === 'B' ? q.B.cy > b.cy : q.B.cy < b.cy)) || (q.B === b && (sd === 'B' ? q.A.cy > b.cy : q.A.cy < b.cy))));
            if (plumb(box, vs) && !plumb(box, vs === 'B' ? 'T' : 'B')) vs = vs === 'B' ? 'T' : 'B';   // a plumb run to an aligned neighbour owns that side
            const k = 'N' + box.st + '/' + Math.round(box.cy) + '/' + vs; (nports[k] = nports[k] || []).push({ r, end: e[0], reach: Math.abs(e[2] - box.cx), vs }); }
          else { const k = key(box) + (e[0] === 'a' ? r.sa : r.sb); (ports[k] = ports[k] || []).push({ r, end: e[0], x: e[2] }); } }); });
      Object.keys(ports).forEach((k) => { const P = ports[k], n = P.length; if (!n) return; const box = P[0].end === 'a' ? P[0].r.A : P[0].r.B; const pf = Math.min(11, Math.max(4, (box.h - 8) / Math.max(1, n - 1)));
        // departures: left lane, top port. arrivals from above: left lane, bottom port; from below: left lane, top port
        P.forEach((p) => { const o = p.end === 'a' ? p.r.A : p.r.B, q = p.end === 'a' ? p.r.B : p.r.A; p.fromAbove = p.end === 'b' && q.cy < o.cy - 1; });
        const aboveN = P.filter((p) => p.fromAbove).length;
        P.sort((p, q) => (aboveN > n / 2 ? -1 : 1) * (p.x - q.x));
        P.forEach((p, i) => { const y = box.cy + (i - (n - 1) / 2) * pf; if (p.end === 'a') p.r.ya = y; else p.r.yb = y; }); });
      Object.keys(nports).forEach((k) => { const P = nports[k], n = P.length; P.sort((p, q) => q.reach - p.reach);   // the furthest reach first: the outer band
        const byBox = {}; P.forEach((p) => { const box = p.end === 'a' ? p.r.A : p.r.B; const kk = box.cx.toFixed(1); (byBox[kk] = byBox[kk] || []).push(p); });
        Object.keys(byBox).forEach((kk) => { const Q = byBox[kk], m = Q.length; Q.forEach((p, j) => { p.dx = (j - (m - 1) / 2) * 4; }); });   // two stubs from one node never share
        P.forEach((p, i) => { const box = p.end === 'a' ? p.r.A : p.r.B; const sgn = p.vs === 'B' ? 1 : -1; const y = box.cy + sgn * (box.h / 2 + 3 + (n - 1 - i) * 4);   /* the row's own half of the gap: the next row's bands take the other half */ if (p.end === 'a') { p.r.ya = y; p.r.na = sgn; p.r.xa = p.dx || 0; } else { p.r.yb = y; p.r.nb = sgn; p.r.xb = p.dx || 0; } }); });
      // the legs
      runs.forEach((r) => { const A = r.A, B = r.B, k = r.lead ? r.lead.rid : G.out.runs; if (!r.lead) r.rid = k;
        const ya = r.ya == null ? (r.lead && r.tend === 'a' ? r.lead.ya : A.cy) : r.ya, yb = r.yb == null ? (r.lead && (r.tend || 'b') === 'b' ? r.lead.yb : B.cy) : r.yb; let Pp;
        // where a run leaves A and enters B: a card by its side at its port; a node by a plumb stub to its band
        const outA = (lx) => (A.node ? [[A.cx + (r.xa || 0), A.cy + (r.na || 1) * (A.h / 2 + 2)], [A.cx + (r.xa || 0), ya]] : [[A.cx + (lx > A.cx ? 1 : -1) * (A.w / 2 + 3), ya]]);
        const inB = (lx) => (B.node ? [[B.cx + (r.xb || 0), yb], [B.cx + (r.xb || 0), B.cy + (r.nb || 1) * (B.h / 2 + 2)]] : [[B.cx + (lx > B.cx ? 1 : -1) * (B.w / 2 + 3), yb]]);
        if (r.same) { if (Math.abs(A.cy - B.cy) < 1) { const l = A.cx < B.cx ? A : B, rr = l === A ? B : A; Pp = [[l.cx + l.w / 2 + 3, A.cy], [rr.cx - rr.w / 2 - 3, A.cy]]; }
          else if (Math.abs(A.cx - B.cx) < 1) { const t = A.cy < B.cy ? A : B, bb = t === A ? B : A; Pp = [[A.cx, t.cy + t.h / 2 + 3], [A.cx, bb.cy - bb.h / 2 - 3]]; }
          else { const lx = r.lx; Pp = outA(lx).concat([[lx, ya], [lx, yb]], inB(lx)); } }
        else if (!r.bus) { if (r.lead && (r.tend || 'b') === 'b') Pp = outA(r.lx).concat([[r.lx, ya], [r.lx, yb]]);          // a member joins the trunk: its own departure, then the lane down to the shared arrival
          else if (r.lead && r.tend === 'a') Pp = [[r.lx, ya], [r.lx, yb]].concat(inB(r.lx));                                 // a branch leaves the trunk: the lane from the shared departure, then its own arrival
          else Pp = outA(r.lx).concat([[r.lx, ya], [r.lx, yb]], inB(r.lx)); }
        else Pp = outA(r.lxA).concat([[r.lxA, ya], [r.lxA, r.yl], [r.lxB, r.yl], [r.lxB, yb]], inB(r.lxB));
        for (let n = 0; n + 1 < Pp.length; n++) { const a = { x: Pp[n][0], y: Pp[n][1] }, b = { x: Pp[n + 1][0], y: Pp[n + 1][1] }; if (Math.abs(a.x - b.x) + Math.abs(a.y - b.y) < 1) continue; if (r.lead && Math.abs(a.x - b.x) < 1 && G.out.edges.some((e) => e.run === k && Math.abs(e.x - a.x) < 0.5 && e.deg % 180 !== 0 && Math.min(a.y, b.y) >= Math.min(e.y, e.y + (e.deg > 0 ? e.len : -e.len)) - 0.5 && Math.max(a.y, b.y) <= Math.max(e.y, e.y + (e.deg > 0 ? e.len : -e.len)) + 0.5)) continue;   // the trunk's lane is drawn once
          G.edge(a, b, r.col, r.cls, r.title); const seg = G.out.edges[G.out.edges.length - 1]; seg.run = k; if (r.joins) seg.joins = r.joins; }
        if (!r.lead) G.out.runs++; });
      runs.length = 0;
    };
    const key = (b) => b.cx.toFixed(1) + ',' + b.cy.toFixed(1);
    return { add, flush };
  }

  /* ── the ISO router: the lattice's own lanes ───────────────────────────────────────────────────────────────────
     Every pin has a ground point (u along the plate, v down it, z the floor). A run leaves its pin along u to a
     corridor in the plate's margin, travels the corridor along v to its target's row, and comes back along u to the
     target's pin — three legs, each changing exactly one ground coordinate, so the run is isometric by construction
     (the design's ISO.route rule). Runs bound down the plate take the LEFT margin, runs bound back up the RIGHT;
     the corridor's lanes are allotted outer-first by reach and the pins' ports fanned in lane order, exactly as the
     cards router does, so nothing overlaps and bundles stay parallel. Relations inside one band are an L (u, then v).
     Pure: the caller hands in the projection and the plate's u-extent. */
  function isoRouter(G) {
    const runs = [];
    const add = (A, B, col, cls, title, joins, o) => { if (A && B) runs.push(Object.assign({ A, B, col, cls: cls || '', title: title || '', joins }, o || {})); };
    const flush = () => {
      const byCor = { L: [], R: [] }, byCol = {};
      const LOC = 112;   // a local lane stands just clear of a column's cards (half a card and a step)
      const trunks = {}; const pk = (p) => p.u.toFixed(1) + ',' + p.v.toFixed(1) + ',' + (p.z || 0);
      runs.forEach((r) => { if (r.direct || r.rel) return; r.flat = Math.abs(r.A.v - r.B.v) < 0.5; if (r.flat) return;
        if (r.trunk) { const k = r.trunk + '|' + (r.tend === 'a' ? pk(r.A) : pk(r.B)); if (trunks[k]) { r.lead = trunks[k]; return; } trunks[k] = r; }
        r.aligned = Math.abs(r.A.u - r.B.u) < 0.5; r.down = r.B.v > r.A.v + 0.5;
        if (r.aligned) { const k = 'C' + r.A.u.toFixed(1) + '/' + (r.A.z || 0); (byCol[k] = byCol[k] || []).push(r); return; }
        r.cor = r.side || (r.down ? 'L' : 'R'); byCor[r.cor].push(r); });
      const reach = (r) => Math.abs(r.B.v - r.A.v) + Math.abs(r.B.u - r.A.u) * 0.001;
      ['L', 'R'].forEach((c) => { const L = byCor[c]; const n = L.length; if (!n) return;
        // outer first: the farthest reach hugs the plate's edge, the shortest sits nearest the items
        L.sort((a, b) => reach(b) - reach(a));
        const pitch = Math.min(G.LP, Math.max(5, (G.MG - 16) / Math.max(1, n - 1)));
        L.forEach((r, i) => { r.lu = c === 'L' ? G.u0 + 8 + i * pitch : G.u0 + G.PW - 8 - i * pitch; r.li = i; }); });
      // the column's own lanes: the farthest reach outermost, so a nearer run's level leg never crosses a farther one
      Object.keys(byCol).forEach((k) => { const L = byCol[k]; L.sort((a, b) => reach(b) - reach(a)); L.forEach((r, i) => { r.lu = r.A.u + LOC + (L.length - 1 - i) * 12; r.li = i; r.cor = 'C'; }); });
      // ports: every pin end joins its run by a PLUMB stub along v to a BAND beside its row, then level along u to the
      // corridor (a relation goes level to its partner's u at the band and plumbs into it — no corridor). A node's band
      // clears the node (16), a card pin's its marker (8). The bands of one row are shared and ordered so that nothing
      // crosses: the pin nearer the corridor takes the band nearer the row, and of the runs leaving ONE pin the one
      // with the furthest reach takes the nearer band (its level leg then passes clear of the inner lanes). Two runs
      // leaving one pin fan their stubs a little along u so they never share the stub.
      const bands = {}; const rowKey = (p, sgn) => Math.round(p.v) + '/' + (p.z || 0) + '/' + sgn;
      const edgeU = (r, p) => (r.cor === 'L' ? G.u0 : r.cor === 'R' ? G.u0 + G.PW : r.lu);
      const want = (r, end) => { const p = end === 'a' ? r.A : r.B, q = end === 'a' ? r.B : r.A; const sgn = q.v > p.v + 0.5 ? 1 : q.v < p.v - 0.5 ? -1 : 1;
        const reach = r.rel ? Math.abs(q.u - p.u) : Math.abs(r.lu - p.u), dist = r.rel ? reach : Math.abs(edgeU(r, p) - p.u);
        (bands[rowKey(p, sgn)] = bands[rowKey(p, sgn)] || []).push({ r, end, p, sgn, reach, dist }); };
      runs.forEach((r) => { if (r.lead) { r.lu = r.lead.lu; r.cor = r.lead.cor; r.aligned = r.lead.aligned; } });
      runs.forEach((r) => { if (r.direct || Math.abs(r.A.v - r.B.v) < 0.5) return; if (!(r.lead && (r.tend || 'b') === 'a')) want(r, 'a'); if (!(r.lead && (r.tend || 'b') === 'b')) want(r, 'b'); });
      Object.keys(bands).forEach((k) => { const P = bands[k]; P.sort((a, b) => (a.dist - b.dist) || (b.reach - a.reach)); const n = P.length;
        const byPin = {}; P.forEach((e) => { const kk = e.p.u.toFixed(1); (byPin[kk] = byPin[kk] || []).push(e); });
        Object.keys(byPin).forEach((kk) => { const Q = byPin[kk], m = Q.length; Q.forEach((e, j) => { e.du = (j - (m - 1) / 2) * 8; }); });
        P.forEach((e, i) => { const base = e.p.node ? 16 : 8; const dv = e.sgn * (base + i * 6); if (e.end === 'a') { e.r.va = dv; e.r.ua = e.du || 0; } else { e.r.vb = dv; e.r.ub = e.du || 0; } }); });
      runs.forEach((r) => { const A = r.A, B = r.B, k = r.lead ? r.lead.rid : G.out.runs; if (!r.lead) r.rid = k; let W;
        if (r.lead) { if (r.va == null) r.va = r.lead.va; if (r.vb == null) r.vb = r.lead.vb; if (r.ua == null) r.ua = r.lead.ua; if (r.ub == null) r.ub = r.lead.ub; }
        if (r.direct || Math.abs(A.v - B.v) < 0.5) W = [[A.u, A.v, A.z], [B.u, B.v, B.z]];                 // along one axis already
        else if (r.rel) { const va = A.v + (r.va || 0), ua = A.u + (r.ua || 0), ub = B.u + (r.ub || 0); W = [[A.u, A.v, A.z], [ua, A.v, A.z], [ua, va, A.z], [ub, va, A.z], [ub, B.v, B.z], [B.u, B.v, B.z]]; }   // out to the band, level to the partner, plumb into it
        else { const va = A.v + (r.va || 0), vb = B.v + (r.vb || 0), ua = A.u + (r.ua || 0), ub = B.u + (r.ub || 0);
          if (r.lead && (r.tend || 'b') === 'b') W = [[A.u, A.v, A.z], [ua, A.v, A.z], [ua, va, A.z], [r.lu, va, A.z], [r.lu, vb, B.z]];   // a member: its departure, then the shared lane to the arrival band
          else if (r.lead && r.tend === 'a') W = [[r.lu, va, A.z], [r.lu, vb, B.z], [ub, vb, B.z], [ub, B.v, B.z], [B.u, B.v, B.z]];       // a branch: the shared lane, then its own arrival
          else W = [[A.u, A.v, A.z], [ua, A.v, A.z], [ua, va, A.z], [r.lu, va, A.z], [r.lu, vb, B.z], [ub, vb, B.z], [ub, B.v, B.z], [B.u, B.v, B.z]]; }
        const Wd = W.filter((q, i) => !i || Math.abs(q[0] - W[i - 1][0]) + Math.abs(q[1] - W[i - 1][1]) + Math.abs((q[2] || 0) - (W[i - 1][2] || 0)) > 0.5);
        const S = Wd.map((q) => G.at(q[0], q[1], q[2]));
        for (let n = 0; n + 1 < S.length; n++) { const a = S[n], b = S[n + 1]; if (Math.hypot(a.x - b.x, a.y - b.y) < 2.5) continue;
          if (r.lead && n === (r.tend === 'a' ? 0 : S.length - 2) && G.out.edges.some((e) => e.run === k && Math.hypot(e.x - a.x, e.y - a.y) < 0.75)) continue;   // the trunk's lane leg is drawn once
          G.edge(a, b, r.col, r.cls, r.title); const seg = G.out.edges[G.out.edges.length - 1]; seg.run = k; if (r.joins) seg.joins = r.joins; }
        if (!r.lead) G.out.runs++; });
      runs.length = 0;
    };
    return { add, flush, pending: () => runs.length };
  }
  /* ── an item as a WIDGET: the form and the data a widget of this kind reads ─────────────────────────────── */
  const SAMPLE = { series: [3, 5, 4, 7, 6, 8, 7], level: { value: 62, max: 100 }, values: { a: 4, b: 7, c: 5, d: 6 }, items: [{ name: 'no reading yet', value: '' }], events: [{ t: '', text: 'no reading yet' }], stages: { steps: [{ label: 'no steps yet', status: '' }] }, string: 'no reading yet', points: [[1, 2], [2, 3], [3, 2]], graph: { nodes: [], links: [] } };
  const SHAPE = { trace: 'series', radial: 'level', counter: 'level', bar: 'level', bars: 'values', thermo: 'values', heat: 'values', matrix: 'values', donut: 'values', stack: 'values', pills: 'values', log: 'events', lane: 'events', table: 'items', files: 'items', list: 'items', checklist: 'items', stepper: 'stages', calendar: 'items', string: 'string', kv: 'values', pipes: 'graph', context_graph: 'graph', scatter: 'points' };
  // the form's own sample face (the widget element's, per form — a radial's, a table's) when the element is on the page;
  // the shape's local sample otherwise. This is what made every placed widget look the same: one sample per SHAPE, so
  // every level was the same dial and every set the same bars, and a form outside SHAPE fell to the one block.
  const sampleOf = (form) => { try { if (root.VeraWidget && typeof root.VeraWidget.sample === 'function') { const s = root.VeraWidget.sample(form); if (s != null) return s; } } catch (_) {} return SAMPLE[SHAPE[form] || 'string'] || SAMPLE.string; };
  function widgetOf(c) {
    c = c || {}; const k = String(c.kind || '').toLowerCase();
    // a placed record (the WidgetConfig sheet's, carried on the card as `record`) says its own form, size and data first
    const rec = c.record && typeof c.record === 'object' ? c.record : null;
    if (rec) { const form = String(rec.form || (rec.draw && rec.draw.form) || c.form || 'kv'); const data = rec.data != null ? rec.data : (c.data != null ? c.data : null);
      return { form, data: data != null ? data : sampleOf(form), sample: data == null, size: String((rec.frame && rec.frame.size) || (rec.draw && rec.draw.size) || ''), record: rec }; }
    if (c.form) return { form: c.form, data: c.data != null ? c.data : sampleOf(c.form), sample: c.data == null };
    if (k === 'widget') { const f = String(c.d || '').replace(/^widget\s*·\s*/, '').trim() || 'kv'; return { form: f, data: c.data != null ? c.data : sampleOf(f), sample: c.data == null }; }
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
  /* ── a widget's OWN face: the registry's renderer (VeraWidget.draw — the one drawer every placement uses) at the
     scene's widget size (S · M · L, else the record's own size), its sample face until it has read — so a radial is a
     radial and a table a table, on the plate, in the cards and in the carousel alike. '' without the widget element on
     the page (the iso group / the widget card stand in). ── */
  const SCREENY = /^(terminal|term|frame|panel|page|notebook|web|browser|chat|dashboard|dash)$/;
  const planeSize = (form, sz) => { const scr = SCREENY.test(String(form || '').toLowerCase()); return scr ? { w: { s: 240, m: 400, l: 600 }[sz], h: { s: 160, m: 280, l: 420 }[sz] } : { w: { s: 150, m: 220, l: 320 }[sz], h: { s: 60, m: 110, l: 170 }[sz] }; };
  function faceHtml(c, wd, wsz, o) {
    if (!(root.VeraWidget && typeof root.VeraWidget.draw === 'function') || !wd || !wd.form) return '';
    const own = wd.size === 'xs' || wd.size === 's' ? 's' : wd.size === 'l' || wd.size === 'xl' ? 'l' : 'm';
    const sz = wsz === 's' || wsz === 'l' ? wsz : (wsz === 'm' ? 'm' : own);
    // on the canvas plane the widget IS the object: it takes the scene's size step, and a screen (a terminal, a panel, a
    // page) is bigger still — a terminal you cannot read is not a terminal
    const pl = o && o.plane ? planeSize(wd.form, sz) : null;
    const H = pl ? Math.round(pl.h) : { s: 24, m: 70, l: 110 }[sz]; let rec = wd.record || null;
    try { if (rec && typeof root.VeraWidget.normalise === 'function') rec = root.VeraWidget.normalise(rec); } catch (_) {}
    let html = ''; try { html = root.VeraWidget.draw(wd.form, wd.data, pl ? (sz === 's' ? 'm' : 'l') : sz, Object.assign({ bare: true, height: H, title: (c && c.n) || wd.form, record: rec, draw: rec && rec.draw, proj: 'iso' }, pl ? { width: Math.round(pl.w), frameMax: 3 } : {})); } catch (_) { html = ''; }
    if (!html) return '';
    return '<div class="xit-face' + (wd.sample ? ' sample' : '') + '" data-form="' + esc(wd.form) + '" data-size="' + sz + '" style="--fh:' + H + 'px">' + html + '</div>';
  }
  // a diagram card's body: its mermaid drawn by the estate's own element (loaded once from the page when a scene needs it)
  const diagramHtml = (c) => { const src = String((c && (c.mermaid || (String(c.kind || '').toLowerCase() === 'diagram' && c.body))) || '').trim(); return src ? '<span class="xf-diag"><vera-mermaid bare title="diagram">' + esc(src) + '</vera-mermaid></span>' : ''; };
  function ensureMermaid(doc) { doc = doc || document; if ((root.customElements && root.customElements.get('vera-mermaid')) || doc.getElementById('vera-mermaid-js')) return; const s = doc.createElement('script'); s.id = 'vera-mermaid-js'; s.src = '/ui/elements/vera_mermaid.js'; s.async = true; (doc.head || doc.documentElement).appendChild(s); }
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
  /* ── FRONT: the relation runs laid out for the carousel — the board's own construction (Canvas.dc.html, xcRels). A
     run leaves a card at its side edge, travels the panel's own gutter, drops to a lane below the cards, runs forward
     (a depth leg), across, back, up, and level into the target — round the outside the whole way, which is what stops a
     run vanishing under the next panel. Every valid run, furthest reach first: that order is the lane order; a run
     needs a lane at BOTH ends and a port on each card side, and two runs leaving one panel never share one, so the
     margin the cards are inset by (pad) is sized from the count rather than guessed. Pure: yOf(li, ci) hands in where a
     card's centre sits in its panel (measured by the element); without it only the pad is answered. ─────────────── */
  function frontRuns(o, yOf) {
    const out = { h: [], v: [], z: [], pad: 42, n: 0 }; const P = (o && o.panels) || [], REL = (o && o.frontRels) || [];
    if (!P.length) return out;
    const PW = P[0].w, PH = P[0].h, li0 = o.layer0 == null ? 2 : o.layer0;
    const ok = REL.map((r) => Math.abs(r.a[0] - li0) <= 2 && Math.abs(r.b[0] - li0) <= 2 && !!(P[r.a[0]] && P[r.a[0]].cards[r.a[1]]) && !!(P[r.b[0]] && P[r.b[0]].cards[r.b[1]]));
    const reach = (r) => Math.abs(r.b[0] - r.a[0]);
    const ORD = REL.map((r, i) => i).filter((i) => ok[i]).sort((x, y) => reach(REL[y]) - reach(REL[x]) || REL[x].a[0] - REL[y].a[0] || x - y);
    const LSRC = {}, LDST = {}, LFLR = {}, cS = {}, cT = {}, PSRC = {}, PDST = {}, pS = {}, pT = {};
    ORD.forEach((i, li) => { const r = REL[i]; LFLR[i] = li; cS[r.a[0]] = (cS[r.a[0]] || 0) + 1; cT[r.b[0]] = (cT[r.b[0]] || 0) + 1; LSRC[i] = cS[r.a[0]] - 1; LDST[i] = cT[r.b[0]] - 1;
      const ks = r.a.join(':'), kt = r.b.join(':'); pS[ks] = (pS[ks] || 0) + 1; pT[kt] = (pT[kt] || 0) + 1; PSRC[i] = [pS[ks] - 1, ks]; PDST[i] = [pT[kt] - 1, kt]; });
    const LMAX = Math.max(1, Math.min(4, Math.max.apply(null, Object.keys(cS).map((k) => cS[k]).concat(Object.keys(cT).map((k) => cT[k])).concat([1]))));
    out.pad = LMAX * 18 + 24; out.n = ORD.length;
    if (typeof yOf !== 'function' || o.focus != null) return out;
    const portY = (base, p, n) => base + (p - (n - 1) / 2) * 11;
    const TH = 26 * Math.PI / 180, CX = Math.cos(TH), SZ = Math.sin(TH);   // the panels' own turn, or the endpoints miss the face
    const HW = PW / 2 - 14 - out.pad;   // the card's edge: the panel's half-width less its padding and the lanes' room
    const settle = 'xp-cs' + li0, settleV = 'xp-csv' + li0;
    const YP = Math.min(8, Math.max(4, Math.floor(60 / Math.max(1, ORD.length - 1)))), ZP = Math.min(10, Math.max(5, Math.floor(80 / Math.max(1, ORD.length - 1))));   // the floor's lane pitch, and its depth pitch: the board's 9 and 12, closer when there are many
    ORD.forEach((i) => { const r = REL[i], A = P[r.a[0]], B = P[r.b[0]];
      const ya0 = yOf(r.a[0], r.a[1]), yb0 = yOf(r.b[0], r.b[1]); if (ya0 == null || yb0 == null) return;
      const ya = portY(ya0, PSRC[i][0], pS[PSRC[i][1]]), yb = portY(yb0, PDST[i][0], pT[PDST[i][1]]);
      const ax = A.px, az = A.pz, bx = B.px, bz = B.pz;
      const GXs = HW + 16 + (LSRC[i] % LMAX) * 18, GXt = HW + 16 + (LDST[i] % LMAX) * 18, li = LFLR[i];
      const yLane = PH / 2 + 30 + (ORD.length - 1 - li) * YP;   // furthest reach lowest
      const dirS = bx >= ax ? 1 : -1, dirT = -dirS;
      const Pq = (cx0, cz0, lx) => ({ x: cx0 + lx * CX, z: cz0 - lx * SZ + 16 });
      const s1 = Pq(ax, az, dirS * HW), s2 = Pq(ax, az, dirS * GXs), t2 = Pq(bx, bz, dirT * GXt), t1 = Pq(bx, bz, dirT * HW);
      const zc = Math.max(s2.z, t2.z) + 170 + (ORD.length - 1 - li) * ZP;   // and deepest
      const col = r.col, cls = r.kind, title = r.title;
      const sg = (p, q, yy) => { const dx = q.x - p.x, dz = q.z - p.z, L = Math.sqrt(dx * dx + dz * dz); if (L < 1) return; out.h.push({ tf: 'translate3d(' + p.x.toFixed(0) + 'px,' + yy.toFixed(0) + 'px,' + p.z.toFixed(0) + 'px) rotateY(' + (Math.atan2(-dz, dx) * 180 / Math.PI).toFixed(1) + 'deg)', w: L.toFixed(0) + 'px', col, cls, title, anim: settle }); };
      const vg = (p, y1, y2) => { if (Math.abs(y2 - y1) < 1) return; out.v.push({ tf: 'translate3d(' + p.x.toFixed(0) + 'px,' + Math.min(y1, y2).toFixed(0) + 'px,' + p.z.toFixed(0) + 'px)', h: Math.abs(y2 - y1).toFixed(0) + 'px', col, cls, title, anim: settleV }); };
      const zg = (p, yy, z1, z2) => { if (Math.abs(z2 - z1) < 1) return; out.z.push({ tf: 'translate3d(' + p.x.toFixed(0) + 'px,' + yy.toFixed(0) + 'px,' + Math.min(z1, z2).toFixed(0) + 'px) rotateX(90deg)', h: Math.abs(z2 - z1).toFixed(0) + 'px', col, cls, title, anim: settleV }); };
      sg(s1, s2, ya); vg(s2, ya, yLane); zg(s2, yLane, s2.z, zc); sg({ x: s2.x, z: zc }, { x: t2.x, z: zc }, yLane); zg(t2, yLane, t2.z, zc); vg(t2, yLane, yb); sg(t2, t1, yb); });
    return out;
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
    for (let i = 1; seen['step:' + i] && seen['step:' + (i + 1)]; i++) rels.push({ from: 'step:' + i, to: 'step:' + (i + 1), kind: 'step' });
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
/* the context chip bar (the board's .ctxbar): the selected turn's layers with their counts — each a toggle — related, and the window's meter */
vera-exploded .xp-ctx{position:absolute;left:14px;top:46px;z-index:30;display:flex;align-items:center;gap:5px;padding:5px 8px;border-radius:8px;background:color-mix(in srgb,var(--xp-s1) 90%,transparent);box-shadow:0 0 0 1px var(--xp-bd)}
vera-exploded .xp-ctx .c{font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--xp-t3);margin-right:3px}
vera-exploded .xp-cb{display:flex;align-items:center;gap:5px;font:inherit;font-size:10px;color:var(--xp-t3);background:none;border:0;cursor:pointer;padding:3px 8px;border-radius:999px}vera-exploded .xp-cb i{width:6px;height:6px;border-radius:2px;background:var(--lc,var(--xp-t3));opacity:.35}vera-exploded .xp-cb b{font-family:var(--xp-mono);font-size:9px;font-weight:400;color:var(--xp-t3)}
vera-exploded .xp-cb.on{color:var(--xp-t1);background:var(--xp-s2)}vera-exploded .xp-cb.on i{opacity:1}vera-exploded .xp-cb:hover{color:var(--xp-t1)}
vera-exploded .xp-cbb{position:relative;font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3);margin-left:6px;padding-left:9px;box-shadow:inset 1px 0 0 0 var(--xp-bd)}vera-exploded .xp-cbb i{position:absolute;left:9px;right:0;bottom:-4px;height:2px;border-radius:1px;background:var(--xp-ac);opacity:.7;max-width:calc(100% - 9px)}
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
/* CARDS — the board's tri-page cards: a rounded plate per layer in the layer's colour, its caption inside the top edge;
   the same .xit as the iso, top-anchored on its row line and growing down. A card has a ceiling; past it the card
   scrolls (one scroll container — no double scrolling with the stage) */
vera-exploded .xp-pl.rect{--pc:var(--xp-ac);border-radius:8px;background:color-mix(in srgb,var(--pc) 6%,var(--xp-s1));box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--pc) 24%,transparent)}
vera-exploded .xp-pl.rect.on{box-shadow:inset 0 0 0 1.5px color-mix(in srgb,var(--pc) 42%,transparent)}
vera-exploded .xp-wrap.cards{cursor:default}vera-exploded .xp-view.scroll{cursor:default}vera-exploded .xp-view.scroll .xp-lb{transform:none}
vera-exploded .xp-lb.hit{pointer-events:auto;cursor:pointer}vera-exploded .xp-lb.hit:hover{text-decoration:underline;text-underline-offset:3px}
vera-exploded .xp-lb.layer.hit{font-size:11px;letter-spacing:.12em}vera-exploded .xp-lb.layer.hit b{font-size:10px;max-width:none}
vera-exploded .xit.ct{transform:none;transform-origin:50% 0;max-height:236px;overflow:auto;scrollbar-width:thin;padding:6px 10px;gap:1px}
vera-exploded .xit.ct .xit-n{-webkit-line-clamp:2;font-size:11.5px;line-height:1.3}vera-exploded .xit.ct .xit-body{margin-top:3px}vera-exploded .xit.ct .xit-m{margin-top:3px}
/* the relevance on the meta line: a short bar and its number, not a body of its own */
vera-exploded .xf-rel{display:inline-flex;align-items:center;gap:5px;margin-left:8px;vertical-align:middle}vera-exploded .xf-rel i{display:inline-block;width:46px;height:3px;border-radius:2px;background:var(--xp-s3);overflow:hidden;position:relative}vera-exploded .xf-rel i b{position:absolute;left:0;top:0;bottom:0;background:var(--cc);border-radius:2px}vera-exploded .xf-rel em{font-style:normal;color:var(--xp-t3)}
/* the stage at its zoom: the wrap is the scroll container, the stage scales about its corner and the wrap's inner box follows */
vera-exploded .xp-stage{position:relative;transform-origin:0 0}
vera-exploded .xit.ct .xit-body{max-height:none;overflow:visible}
vera-exploded .xit-c{font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3);background:var(--xp-s2);border-radius:999px;padding:1px 7px}
vera-exploded .xit-xr{display:flex;gap:8px;font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3)}vera-exploded .xit-xr b{margin-left:auto;color:var(--xp-t2);font-weight:400}
/* the plate's edges: in iso a plane is a polygon clipped out of a layer, so its outline is drawn as four lines in the
   plate's colour (the board's .xpe) — they are what keeps it a plane when Blocks takes the fill away */
vera-exploded .xp-pe{position:absolute;height:1px;transform-origin:0 50%;pointer-events:none;z-index:3;background:color-mix(in srgb,var(--pc,var(--xp-ac)) 34%,transparent)}
vera-exploded .xp-be{display:none;position:absolute;height:0;border-top:1px dashed color-mix(in srgb,var(--bc,var(--xp-ac)) 45%,transparent);transform-origin:0 50%;pointer-events:none;z-index:3}
vera-exploded .xp-lb{position:absolute;font-size:10px;letter-spacing:.12em;text-transform:uppercase;white-space:nowrap;display:flex;gap:8px;align-items:baseline;pointer-events:none;color:var(--xp-t3)}
vera-exploded .xp-lb b{font-family:var(--xp-mono);font-size:9.5px;letter-spacing:0;font-weight:400;text-transform:none;color:var(--xp-t3);max-width:320px;overflow:hidden;text-overflow:ellipsis}
vera-exploded .xp-lb.station{font-size:11px;color:var(--xp-t2);pointer-events:auto;cursor:pointer}vera-exploded .xp-lb.station.on{color:var(--xp-t1)}vera-exploded .xp-lb.station:hover{text-decoration:underline;text-underline-offset:3px}
vera-exploded .xp-lb.sm{font-size:8.5px}
vera-exploded .xp-e{position:absolute;height:2px;transform-origin:0 50%;z-index:6;pointer-events:none;border-radius:2px;background:var(--ec);opacity:.75;box-shadow:0 0 7px -2px var(--ec)}
vera-exploded .xp-e.mem,vera-exploded .xp-e.pin,vera-exploded .xp-e.dash{height:0;border-top:2px dashed var(--ec);background:none;box-shadow:none}
/* The relation edges answer to the tier (defect 83): Full draws every one of them, as it always did; Hover and Zen
   rest them and light the ones touching whatever the pointer is on. data-rels="off" puts them away in every tier -
   the header's third switch, beside Context -> chat and Chat <-> canvas. Only the RELATIONS answer to this: the runs
   that carry the turn's own story (read · said · made · landed) are the scene itself and are never hidden. */
vera-exploded[data-den="hover"] .xp-e.rel,vera-exploded[data-den="zen"] .xp-e.rel{opacity:0;transition:opacity .13s ease}
vera-exploded[data-den="hover"] .xp-e.rel.hot,vera-exploded[data-den="zen"] .xp-e.rel.hot{opacity:.7}
vera-exploded[data-rels="off"] .xp-e.rel{display:none}
vera-exploded .xp-e.thin{height:1px;opacity:.5;box-shadow:none}vera-exploded .xp-e.rel{height:1px;opacity:.55;box-shadow:none;z-index:7}vera-exploded .xp-e.rel.cite{opacity:.4}vera-exploded .xp-e.rel.mem{height:0;border-top:1px dashed var(--ec);background:none}
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
vera-exploded .xp-pz{position:absolute;left:14px;bottom:12px;z-index:34;display:flex;align-items:center;gap:3px;padding:4px 6px;border-radius:8px;background:color-mix(in srgb,var(--xp-s1) 90%,transparent);box-shadow:0 0 0 1px var(--xp-bd);opacity:.6}vera-exploded .xp-pz:hover{opacity:1}vera-exploded .xp-pz .sp{width:1px;height:14px;background:var(--xp-bd);margin:0 3px}vera-exploded .xp-pz .tl{display:flex;align-items:center;gap:3px}vera-exploded .xp-ctl .iso-c{margin-left:2px}
vera-exploded .xp-pz button{font:inherit;font-family:var(--xp-mono);font-size:11px;color:var(--xp-t2);background:none;border:0;cursor:pointer;padding:3px 7px;border-radius:4px}vera-exploded .xp-pz button:hover{background:var(--xp-s2);color:var(--xp-t1)}vera-exploded .xp-pz .z{font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3);min-width:34px;text-align:center}
/* FRONT: the carousel — the chat UI's own construction, ported rather than imitated: the layers pulled apart along one
   axis, the one you are on nearest and centred, the rest receding either side, every face turned the same little way
   off axis so each is read head-on. A panel is sized from the room the view has (inline); the ones past the window fade
   to a ghost — plainly continuing, not competing */
/* the carousel sits a little above the middle: the runs' floor lanes lie below the panels, and the host's composer floats over the foot */
vera-exploded .xp-car{position:absolute;inset:0;perspective:2800px;perspective-origin:50% 42%;overflow:hidden}
vera-exploded .xp-track{position:absolute;left:50%;top:42%;width:0;height:0;transform-style:preserve-3d;transition:transform .5s cubic-bezier(.2,.85,.25,1.02)}
vera-exploded .xp-cp{position:absolute;left:-200px;top:-236px;width:400px;height:472px;border-radius:8px;cursor:pointer;display:flex;flex-direction:column;gap:9px;padding:14px 14px 16px;box-sizing:border-box;overflow:hidden;background:color-mix(in srgb,var(--pc) 8%,var(--xp-s1));box-shadow:0 0 0 1px color-mix(in srgb,var(--pc) 32%,transparent),0 calc(var(--el,1) * 4px + 4px) calc(var(--el,1) * 10px + 12px) calc(var(--el,1) * -3px - 10px) rgba(0,0,0,.9);will-change:transform;transition:transform .64s cubic-bezier(.2,.85,.25,1.04),opacity .46s ease,box-shadow .46s ease,filter .46s ease;animation:xp-cp-in .62s cubic-bezier(.2,.8,.25,1) backwards;animation-delay:var(--d,0s)}
/* 2D → 3D: the panels start flat and face-on, then swing into the carousel */
@keyframes xp-cp-in{from{opacity:0;transform:translateX(0) translateZ(-140px) rotateY(-6deg)}}
vera-exploded .xp-cp.on{background:var(--xp-s1);box-shadow:0 0 0 1.5px color-mix(in srgb,var(--pc) 62%,transparent),0 22px 40px -26px #000}vera-exploded .xp-cp.near{opacity:.92}vera-exploded .xp-cp.far{opacity:.62;filter:brightness(.82)}
/* past the window: plainly continuing, not competing */
vera-exploded .xp-cp.gone{opacity:.16;pointer-events:none;filter:brightness(.6)}vera-exploded .xp-cp.gone .xp-cp-b{visibility:hidden}
vera-exploded .xp-cp-h{display:flex;align-items:baseline;gap:7px;font-size:9px;letter-spacing:.15em;text-transform:uppercase;color:color-mix(in srgb,var(--pc) 90%,var(--xp-t3));flex-shrink:0;cursor:pointer}vera-exploded .xp-cp-h:hover{color:var(--xp-t1)}vera-exploded .xp-cp-h i{width:7px;height:7px;border-radius:2px;background:var(--pc);align-self:center}vera-exploded .xp-cp-h .sub{letter-spacing:0;text-transform:none;opacity:.7}vera-exploded .xp-cp-h b{font-family:var(--xp-mono);font-size:9px;letter-spacing:0;color:var(--xp-t3);font-weight:400;text-transform:none;margin-left:auto}vera-exploded .xp-cp-h .fx2{margin-left:8px;font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3);letter-spacing:0;text-transform:none}
/* the cards are inset by exactly the lanes the runs need (--cpad, sized from the relation count), so a run has somewhere
   to leave from and the scrollbar never lands on a lane; the body scrolls so an opened card makes room instead of being clipped */
vera-exploded .xp-cp-b{flex:1;min-height:0;display:flex;flex-direction:column;gap:14px;overflow-y:auto;overflow-x:hidden;scrollbar-width:thin;padding:0 var(--cpad,29px) 2px}
/* a layer with many cards: two columns, the header says how many, and the thread fades out at the foot */
vera-exploded .xp-cp.many .xp-cp-b{display:grid;grid-template-columns:1fr 1fr;gap:10px;align-content:start;padding-bottom:22px;-webkit-mask-image:linear-gradient(#000 calc(100% - 28px),transparent);mask-image:linear-gradient(#000 calc(100% - 28px),transparent)}
/* focusing a whole section: a layer's header brings that entire card square-on and full size, and hushes the rest; clicking it again puts it back in the line */
vera-exploded .xp-cp.focus{width:860px!important;height:560px!important;left:-430px!important;top:-280px!important;z-index:40;background:var(--xp-s1);box-shadow:0 0 0 1.5px color-mix(in srgb,var(--pc) 62%,transparent),0 34px 72px -30px #000}
vera-exploded .xp-cp.focus .xp-cp-b{display:grid;grid-template-columns:1fr 1fr;gap:10px;align-content:start;overflow:auto;-webkit-mask-image:none;mask-image:none}
vera-exploded .xp-cp.hushed{opacity:.12;pointer-events:none}
/* the flow variant of the card: the same inner vocabulary as the projected scene, laid out in a column */
vera-exploded .xp-rc{position:relative;display:flex;flex-direction:column;gap:2px;padding:8px 10px;border-radius:6px;background:var(--xp-s2);cursor:pointer;overflow:hidden;box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-bd);transition:box-shadow .15s ease,transform .15s ease;animation:xp-rc-in .42s ease backwards;animation-delay:.2s}
@keyframes xp-rc-in{from{opacity:0;transform:translateY(8px)}}
vera-exploded .xp-rc:hover{box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-bd2);transform:translateX(2px)}vera-exploded .xp-rc.open{box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-ac);z-index:3}
vera-exploded .xp-rc .n{font-size:11px;color:var(--xp-t1)}vera-exploded .xp-rc .d{font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3)}vera-exploded .xp-rc .b{display:none;margin-top:4px;font-size:10px;color:var(--xp-t2)}vera-exploded .xp-rc.open .b{display:block}vera-exploded .xp-rc .b pre{margin:0;font-family:var(--xp-mono);font-size:9px;white-space:pre-wrap}
/* the leaders that make it read as pulled apart rather than merely spaced; the spine runs the length of the carousel,
   so the layers plainly carry on past the edge of the view */
vera-exploded .xp-spine{position:absolute;left:-2600px;top:-1px;width:5200px;height:2px;pointer-events:none;z-index:0;background:linear-gradient(90deg,transparent,color-mix(in srgb,var(--xp-t3) 26%,transparent) 20%,color-mix(in srgb,var(--xp-t3) 26%,transparent) 80%,transparent)}
vera-exploded .xp-spine i{position:absolute;left:50%;top:-3px;width:8px;height:8px;margin-left:-4px;border-radius:50%;background:var(--xp-ac);opacity:.55}
vera-exploded .xp-lead{position:absolute;left:0;top:0;height:0;transform-origin:0 50%;border-top:1px dashed var(--xp-bd2);pointer-events:none;z-index:1}vera-exploded .xp-lead.across{border-top-color:color-mix(in srgb,var(--xp-ac) 55%,transparent);border-top-style:dotted}
/* the relation runs, laid out for the carousel (frontRuns): out of the card's side edge, into the panel's own gutter,
   down to a lane below the cards, forward — a depth leg, its HEIGHT axis mapped onto Z so it runs toward the reader —
   across, back and up. A run is recomputed the moment the layer changes, so while the panels glide it would be drawn
   at the NEW geometry against the OLD positions — the disconnected, janky look. The runs are held back until the
   panels land, then fade in attached; the keyframe name varies with the focused layer so the hold replays on every change */
vera-exploded .xp-runs{position:absolute;left:0;top:0;width:0;height:0;transform-style:preserve-3d}
vera-exploded .xp-cr,vera-exploded .xp-crv,vera-exploded .xp-crz{animation-duration:.92s;animation-timing-function:cubic-bezier(.3,.7,.25,1);animation-fill-mode:backwards;pointer-events:none;z-index:2;border-radius:2px}
vera-exploded .xp-cr{position:absolute;left:0;top:0;height:0;transform-origin:0 50%;border-top:2px solid var(--xp-t3)}vera-exploded .xp-cr.mem{border-top-style:dashed}
vera-exploded .xp-crv{position:absolute;left:0;top:0;width:2px}
vera-exploded .xp-crz{position:absolute;left:0;top:0;width:2px;transform-origin:50% 0}
@keyframes xp-cs0{0%,68%{opacity:0;clip-path:inset(0 100% 0 0)}70%{opacity:1;clip-path:inset(0 100% 0 0)}100%{opacity:1;clip-path:inset(0 0 0 0)}}
@keyframes xp-cs1{0%,68%{opacity:0;clip-path:inset(0 100% 0 0)}70%{opacity:1;clip-path:inset(0 100% 0 0)}100%{opacity:1;clip-path:inset(0 0 0 0)}}
@keyframes xp-cs2{0%,68%{opacity:0;clip-path:inset(0 100% 0 0)}70%{opacity:1;clip-path:inset(0 100% 0 0)}100%{opacity:1;clip-path:inset(0 0 0 0)}}
@keyframes xp-cs3{0%,68%{opacity:0;clip-path:inset(0 100% 0 0)}70%{opacity:1;clip-path:inset(0 100% 0 0)}100%{opacity:1;clip-path:inset(0 0 0 0)}}
@keyframes xp-cs4{0%,68%{opacity:0;clip-path:inset(0 100% 0 0)}70%{opacity:1;clip-path:inset(0 100% 0 0)}100%{opacity:1;clip-path:inset(0 0 0 0)}}
@keyframes xp-csv0{0%,68%{opacity:0;clip-path:inset(0 0 100% 0)}70%{opacity:1;clip-path:inset(0 0 100% 0)}100%{opacity:1;clip-path:inset(0 0 0 0)}}
@keyframes xp-csv1{0%,68%{opacity:0;clip-path:inset(0 0 100% 0)}70%{opacity:1;clip-path:inset(0 0 100% 0)}100%{opacity:1;clip-path:inset(0 0 0 0)}}
@keyframes xp-csv2{0%,68%{opacity:0;clip-path:inset(0 0 100% 0)}70%{opacity:1;clip-path:inset(0 0 100% 0)}100%{opacity:1;clip-path:inset(0 0 0 0)}}
@keyframes xp-csv3{0%,68%{opacity:0;clip-path:inset(0 0 100% 0)}70%{opacity:1;clip-path:inset(0 0 100% 0)}100%{opacity:1;clip-path:inset(0 0 0 0)}}
@keyframes xp-csv4{0%,68%{opacity:0;clip-path:inset(0 0 100% 0)}70%{opacity:1;clip-path:inset(0 0 100% 0)}100%{opacity:1;clip-path:inset(0 0 0 0)}}
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
/* a widget's own face on the card (defect 37): the registry's drawing at the scene's size; the sample face a little quieter */
vera-exploded .xit-face{display:block;margin-top:4px;min-height:var(--fh,70px);overflow:hidden;border-radius:4px;flex-shrink:0}vera-exploded .xit-face[data-size="s"]{min-height:0}vera-exploded .xit-face.sample{opacity:1;position:relative}vera-exploded .xit-face.sample::after{content:'sample';position:absolute;right:3px;top:3px;font-family:var(--xp-mono);font-size:8px;color:var(--xp-t3);background:color-mix(in srgb,var(--xp-s1) 80%,transparent);border-radius:999px;padding:0 5px;opacity:0;pointer-events:none}vera-exploded .xit:hover .xit-face.sample::after,vera-exploded .xigf:hover .xit-face.sample::after{opacity:1}
vera-exploded .xit-face .vw-sampled,vera-exploded .xit-face .vw-form,vera-exploded .xit-face .vw-body{display:block}
vera-exploded .xit.tight .xit-face{display:none}vera-exploded .xit.tight.open .xit-face,vera-exploded .xit.tight:hover .xit-face{display:block}
vera-exploded .xp-rc .xit-face{margin-top:6px}
/* a diagram card: the estate's mermaid element on the card, sized for a card */
vera-exploded .xf-diag{display:block;margin-top:3px}vera-exploded .xf-diag vera-mermaid{display:block;width:100%;height:110px;min-height:0}vera-exploded .xit.ct .xf-diag vera-mermaid{height:150px}
vera-exploded .xp-rc .b .xf-diag vera-mermaid,vera-exploded .xp-it .b .xf-diag vera-mermaid{height:130px}
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
/* a widget with a face of its own stands on the plate as itself: no frame, no ground, the label box beneath (defect 79) */
vera-exploded .xigf{width:var(--xw,190px);height:auto;transform-origin:50% 100%;transform:translate(-50%,-100%) scale(var(--inv,1))}
vera-exploded .xigf .xit-face{background:transparent!important;box-shadow:none!important;border-radius:0;padding:0;margin:0}
vera-exploded .xigf.open{z-index:27}
vera-exploded .xig.sample{opacity:1}
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
/* Blocks off in the exploded scene, the board's words: cards, plates, panels and columns lose their FILLS — outlines
   only. So the iso plate keeps its four edges (drawn as lines, and brighter now) and the plane stays a plane with its
   ground legible; the bands turn to hairline dashed outlines; a card keeps its accent bar and a faint ring; the widgets
   stay. Nothing is removed, only the fills. */
:root[data-blocks="off"] vera-exploded .xp-pl,:root[data-blocks="off"] vera-exploded .xp-band{background:none!important}
:root[data-blocks="off"] vera-exploded .xp-pl.rect{box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--pc) 34%,transparent)}
:root[data-blocks="off"] vera-exploded .xp-pe{background:color-mix(in srgb,var(--pc,var(--xp-ac)) 62%,transparent);box-shadow:0 0 6px -1px color-mix(in srgb,var(--pc,var(--xp-ac)) 40%,transparent)}
:root[data-blocks="off"] vera-exploded .xp-be{display:block}
:root[data-blocks="off"] vera-exploded .xit,:root[data-blocks="off"] vera-exploded .xp-it,:root[data-blocks="off"] vera-exploded .xp-cp,:root[data-blocks="off"] vera-exploded .xp-rc,:root[data-blocks="off"] vera-exploded .xp-g{background:transparent!important}
:root[data-blocks="off"] vera-exploded .xit{box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px color-mix(in srgb,var(--xp-bd) 70%,transparent)}
:root[data-blocks="off"] vera-exploded .xit.open{background:color-mix(in srgb,var(--xp-s1) 72%,transparent)!important;box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1.5px var(--xp-ac)}
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
/* GENERATING: the chat's throbber on the exchange while the reply is on its way — a breathing orbit and the rolling phrase
   (chat_panel.html .think-throb, the same idiom). Held back, then attached: the card arrives after the scene has settled and
   breathes until the reply lands; the exchange's plate, panel and band carry a soft pulsing ring meanwhile */
@keyframes xp-orbit{to{transform:rotate(360deg)}}@keyframes xp-breathe{0%,100%{opacity:.45;transform:scale(.88)}50%{opacity:1;transform:scale(1)}}@keyframes xp-phrase-in{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
@keyframes xp-gen-in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}@keyframes xp-gen-ring{0%,100%{box-shadow:inset 0 0 0 1.5px color-mix(in srgb,var(--xp-ac2) 30%,transparent)}50%{box-shadow:inset 0 0 0 1.5px color-mix(in srgb,var(--xp-ac2) 70%,transparent)}}
vera-exploded .xp-throb{display:inline-flex;align-items:center;gap:7px;color:var(--xp-ac2);font-size:10px;min-height:16px;max-width:100%}
vera-exploded .xp-throb .orb{position:relative;width:14px;height:14px;flex-shrink:0;animation:xp-breathe 2.4s ease-in-out infinite}
vera-exploded .xp-throb .orb::before{content:'';position:absolute;inset:0;border-radius:50%;border:1.5px solid var(--xp-ac2);border-top-color:transparent;border-left-color:transparent;animation:xp-orbit 1.1s linear infinite}
vera-exploded .xp-throb .orb::after{content:'';position:absolute;left:50%;top:50%;width:4px;height:4px;margin:-2px 0 0 -2px;border-radius:50%;background:var(--xp-ac)}
vera-exploded .xp-throb .phrase{font-style:italic;opacity:.95;color:var(--xp-t1);animation:xp-phrase-in .5s ease;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
vera-exploded .xit.gen,vera-exploded .xp-rc.gen{animation:xp-gen-in .5s ease backwards;animation-delay:.45s;border:1px dashed color-mix(in srgb,var(--xp-ac2) 55%,transparent);box-shadow:inset 3px 0 0 0 var(--cc)}vera-exploded .xit.gen .xit-n,vera-exploded .xp-rc.gen .n{display:none}vera-exploded .xit.gen .xit-d,vera-exploded .xp-rc.gen .d{color:var(--xp-ac2);opacity:.8}
vera-exploded .xit.gen .xit-body,vera-exploded .xp-rc.gen .b{display:flex!important;max-height:none;opacity:1;margin-top:4px}vera-exploded .xit.gen.bb{animation-name:xp-gen-in}
vera-exploded .xp-pl.gen,vera-exploded .xp-cp.gen{animation:xp-gen-ring 2.2s ease-in-out infinite}vera-exploded .xp-pl.rect.gen{background:color-mix(in srgb,var(--xp-ac2) 5%,var(--xp-s1))}
vera-exploded .xp-band.gen{background:color-mix(in srgb,var(--xp-ac2) 12%,transparent);animation:xp-breathe 2.4s ease-in-out infinite}
:root[data-blocks="off"] vera-exploded .xp-pl.rect.gen,:root[data-blocks="off"] vera-exploded .xp-band.gen{background:none!important}
/* the two optional layers: a call is a typed node in the activity lane (ok · running · failed by colour), an estate node a
   host, a container or a service; their runs carry their own kind so the key can name them */
vera-exploded .xnd.act{border-radius:5px}vera-exploded .xnd.est{border-radius:4px;box-shadow:inset 0 0 0 1.4px var(--nc),0 0 0 2px color-mix(in srgb,var(--nc) 14%,transparent)}
vera-exploded .xnd.act.run{animation:xp-breathe 1.6s ease-in-out infinite}
vera-exploded .xp-e.act{opacity:.7}vera-exploded .xp-e.est{opacity:.7}vera-exploded .xp-e.link.dash{height:0;border-top:2px dashed var(--ec);background:none;box-shadow:none}
vera-exploded .xp-band.estate{background:color-mix(in srgb,var(--bc) 5%,transparent)}
/* a widget is picked up: it follows the pointer, the plate it can be dropped on lights, the item it would go before lifts a ring */
vera-exploded [data-drag]{cursor:grab}vera-exploded .xp-lift{cursor:grabbing!important;z-index:60!important;opacity:.92;transition:none!important;pointer-events:none}
vera-exploded .xp-pl.drop{box-shadow:inset 0 0 0 2px var(--xp-ac),0 0 0 4px color-mix(in srgb,var(--xp-ac) 22%,transparent)!important;background:color-mix(in srgb,var(--xp-ac) 12%,transparent)!important}
vera-exploded .xp-drop-before{box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 2px var(--xp-ac)!important}
vera-exploded .xp-wrap.dropping{cursor:grabbing}vera-exploded .xp-wrap.dropping .xp-pl{pointer-events:auto}
/* the edit affordance on a widget you placed: a small gear, shown as you point at it */
vera-exploded .xit-edit{position:absolute;right:4px;top:4px;width:16px;height:16px;border:0;border-radius:4px;background:var(--xp-s3);color:var(--xp-t2);font-size:10px;line-height:16px;padding:0;cursor:pointer;opacity:0;z-index:3;transition:opacity .12s ease}
vera-exploded .xit:hover .xit-edit,vera-exploded .xit.open .xit-edit{opacity:1}vera-exploded .xit-edit:hover{color:var(--xp-t1);background:var(--xp-bd2)}
vera-exploded .xit.frameless{position:absolute}vera-exploded .xit.frameless .xit-edit{position:static;margin-left:4px;width:14px;height:14px;line-height:14px;align-self:center}
`;
  function ensureCss(doc) { doc = doc || document; if (doc.getElementById('vera-exploded-css')) return; const s = doc.createElement('style'); s.id = 'vera-exploded-css'; s.textContent = CSS; (doc.head || doc.documentElement).appendChild(s); }
  // the shared ISO projection (/ui/iso.js, window.VeraISO) draws the widget groups; a page that has not loaded it gets it
  // once, here, and the scene redraws when it lands — without it the widgets stay flat cards (the board's widget card)
  function ensureIso(doc, onload) { doc = doc || document; if (root.VeraISO || doc.getElementById('vera-iso-lib')) return; const s = doc.createElement('script'); s.id = 'vera-iso-lib'; s.src = '/ui/iso.js'; s.async = true; s.onload = () => { try { onload && onload(); } catch (_) {} }; (doc.head || doc.documentElement).appendChild(s); }
  // a preference remembered per browser (the layers, the galaxy) — absent storage reads as the default
  const readPref = (k, dflt) => { try { const v = root.localStorage && root.localStorage.getItem(k); if (v == null) return dflt; const j = JSON.parse(v); return (dflt && typeof dflt === 'object') ? Object.assign({}, dflt, j || {}) : j; } catch (_) { return dflt; } };
  const writePref = (k, v) => { try { root.localStorage && root.localStorage.setItem(k, JSON.stringify(v)); } catch (_) {} };
  const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  // GENERATING — the same throbber the chat draws while a reply is on its way (its .think-throb: a breathing orbit
  // spinner and the rolling thinking phrase), on the exchange's card in every mode
  const throbHtml = (c) => '<span class="xp-throb" role="status"><span class="orb"></span><span class="phrase">' + esc((c && c.n) || 'Generating…') + '</span></span>';
  function cardBody(c) {
    if (String((c && c.kind) || '').toLowerCase() === 'gen') return throbHtml(c);
    const k = c.kind || ''; let h = '';
    if (c.src) h += '<img class="xp-img" src="' + esc(c.src) + '" alt="" loading="lazy">';
    if (c.score != null) h += '<div class="bar"><i style="width:' + Math.round(Math.max(0, Math.min(1, +c.score)) * 100) + '%"></i></div>';
    if (k === 'diff' && (c.p != null || c.m != null)) h += '<div class="pm"><b class="p">+' + esc(c.p || 0) + '</b><b class="m">−' + esc(c.m || 0) + '</b></div>';
    if (Array.isArray(c.rows) && c.rows.length) h += '<div class="kv">' + c.rows.slice(0, 8).map((r) => '<b>' + esc(r.k) + '</b><span>' + esc(r.v) + '</span>').join('') + '</div>';
    if (Array.isArray(c.steps) && c.steps.length) h += '<div class="steps">' + c.steps.slice(0, 12).map((s) => '<span class="' + esc(s.status || '') + '"><i></i>' + esc(s.label || s.n || '') + (s.cap ? ' · ' + esc(s.cap) : '') + '</span>').join('') + '</div>';
    if (String(k).toLowerCase() === 'diagram') { const d = diagramHtml(c); if (d) { h += d; return h; } }
    if (c.body) h += '<pre>' + esc(String(c.body).slice(0, 600)) + '</pre>';
    return h;
  }
  /* ── the ISO card's body, the board's vocabulary by kind: what shows on the card, and what waits behind a click ── */
  function isoBody(c, wd) {
    c = c || {}; const k = String(c.kind || '').toLowerCase(); let on = '', x = '';
    if (k === 'gen') return { on: throbHtml(c), x: '' };   // the generating state: the throbber, nothing behind a click
    const codeish = /^(code|term|terminal|cap|capability|log)$/.test(k); const lines = String(c.body || '').split('\n').filter((l) => l.trim());
    if (c.score != null && !codeish && !/^(image|widget|artifact|note|loop|diff)$/.test(k)) on += '<span class="xf-score"><span class="xf-bar"><i style="width:' + Math.round(Math.max(0, Math.min(1, +c.score)) * 100) + '%"></i></span><b>' + (+c.score).toFixed(2) + '</b></span>';
    if (k === 'diff' && (c.p != null || c.m != null)) on += '<span class="xf-diff"><b class="p">+' + esc(c.p || 0) + '</b><b class="m">−' + esc(c.m || 0) + '</b></span>';
    if (Array.isArray(c.rows) && c.rows.length) { const tr = (r) => '<span class="xf-tr">' + esc(r.k) + '<b>' + esc(r.v) + '</b></span>'; on += '<span class="xf-tab">' + c.rows.slice(0, 3).map(tr).join('') + '</span>'; if (c.rows.length > 3) x += '<span class="xf-tab">' + c.rows.slice(3, 12).map(tr).join('') + '</span>'; }
    if (Array.isArray(c.steps) && c.steps.length) { const ls = (s, i) => '<span class="xf-ls ' + esc(s.status || '') + '"><i></i><b>' + (i + 1) + '</b><span>' + esc(s.label || s.n || '') + (s.cap ? ' · ' + esc(s.cap) : '') + '</span></span>'; on += '<span class="xf-loop">' + c.steps.slice(0, 3).map(ls).join('') + '</span>'; if (c.steps.length > 3) x += '<span class="xf-loop">' + c.steps.slice(3, 12).map((s, i) => ls(s, i + 3)).join('') + '</span>'; }
    if (Array.isArray(c.bars) && c.bars.length) { const vs = c.bars.map((b) => typeof b === 'number' ? b : parseFloat(b) || 0), mx = Math.max.apply(null, vs) || 1; on += '<span class="xf-chart">' + vs.slice(0, 12).map((v) => '<i style="height:' + Math.round(v / mx * 100) + '%"></i>').join('') + '</span>'; }
    if (wd && (k === 'widget' || c.form || c.tpl)) { // a widget without the iso lib: the board's widget card — the reading, its source, a sparkline
      const vs = seriesOf(wd), mx = Math.max.apply(null, vs.map((v) => Math.abs(v))) || 1; on += '<span class="xf-w"><b>' + esc(valueOf(wd) || (wd.sample ? '—' : '')) + '</b><span class="xf-wd">' + esc(wd.form) + (wd.sample ? ' · no reading yet' : '') + '</span></span>' + (vs.length > 1 ? '<span class="xf-ws">' + vs.map((v) => '<i style="height:' + Math.max(8, Math.round(Math.abs(v) / mx * 100)) + '%"></i>').join('') + '</span>' : ''); }
    if (codeish && lines.length) { on += '<span class="' + (k === 'code' ? 'xf-code' : 'xf-term') + '">' + esc(lines[0].slice(0, 80)) + '</span>'; if (lines.length > 1) x += '<pre>' + esc(String(c.body).slice(0, 600)) + '</pre>'; }
    else if (k === 'diagram' && diagramHtml(c)) { on += diagramHtml(c); x += '<pre>' + esc(String(c.mermaid || c.body || '').slice(0, 600)) + '</pre>'; }   // the diagram on the card, its source behind the click
    else if (c.body && !c.rows) x += '<pre>' + esc(String(c.body).slice(0, 600)) + '</pre>';
    return { on, x };
  }

  if (typeof HTMLElement !== 'undefined' && root.customElements && !root.customElements.get('vera-exploded')) {
    class VeraExploded extends HTMLElement {
      constructor() { super(); this._S = { scene: { turns: [], sel: '' }, mode: 'cards', layer: 2, open: null, focus: null, heights: {}, related: true, lanesOff: {}, budget: null, solo: false, tilt: 30, azim: 45, pan: { x: 0, y: 0, z: 1 }, layers: readPref('vera_xpl_layers', { activity: false, estate: false }), galaxy: readPref('vera_xpl_galaxy', true) !== false }; this._raf = 0; }
      // the two layers and the galaxy are the user's choice, remembered per browser
      layers(v) { if (v && typeof v === 'object') { this._S.layers = { activity: !!v.activity, estate: !!v.estate }; writePref('vera_xpl_layers', this._S.layers); this._schedule(); this._layersEv(); } return this._S.layers; }
      galaxy(on) { this._S.galaxy = on == null ? !this._S.galaxy : !!on; writePref('vera_xpl_galaxy', this._S.galaxy); this._schedule(); this._layersEv(); return this._S.galaxy; }
      _layersEv() { this.dispatchEvent(new CustomEvent('vera:xpl:layers', { detail: { activity: !!this._S.layers.activity, estate: !!this._S.layers.estate, galaxy: !!this._S.galaxy }, bubbles: true })); }
      connectedCallback() {
        ensureCss(this.ownerDocument); ensureIso(this.ownerDocument, () => this._schedule()); if (this._built) { this._schedule(); return; } this._built = true;
        const m = this.getAttribute('mode'); if (m) this._S.mode = m;
        this.innerHTML = '<div class="xp-ctl"><span class="c">explode</span><button data-m="cards">Cards</button><button data-m="front">Front</button><button data-m="iso">Iso</button><span class="sep"></span><button data-a="fit" title="Back to the whole scene">Fit</button><button data-a="close" title="Back to the flat transcript">Flatten</button><span class="sep"></span><span class="c iso-c" data-r="isoc">iso</span><button data-a="solo" title="Only this turn — the selected turn\'s plate alone (the board\'s single-layer iso)">Turn</button><button data-a="all" title="Every turn — the plates in a row">All</button><button data-a="stack" title="Stack — the stations on floors, one above the other">Stack</button><button data-a="wsz" title="The iso widgets\' size — S · M · L">M</button><span class="sep"></span><button data-a="place" title="Place a widget from the registry onto this station\'s plate — it becomes one of the turn\'s items, tagged ⧉ with its template">+ Place</button><span class="sep"></span><input type="range" class="xp-scrub" data-r="scrub" min="0" max="0" value="0" title="Scrub through the session\'s turns (← → too)"></div><div class="xp-ctx" data-r="ctx"></div><div class="xp-dots" data-r="dots"></div><div class="xp-wrap" data-r="wrap"><div class="xp-view" data-r="view"></div></div><div class="xp-pz"><button data-a="zout" title="Zoom out">−</button><span class="z" data-r="zoom">100%</span><button data-a="zin" title="Zoom in">+</button><span class="sp"></span><button data-a="panl" title="Pan left">←</button><button data-a="panu" title="Pan up">↑</button><button data-a="pand" title="Pan down">↓</button><button data-a="panr" title="Pan right">→</button><span class="tl" data-r="tl"><span class="sp"></span><button data-a="tiltu" title="Tilt the view up — look down on the plane">⌃</button><span class="z" data-r="ang">30°</span><button data-a="tiltd" title="Tilt the view down — flatten the plane">⌄</button><button data-a="swl" title="Swing the view left">↺</button><button data-a="swr" title="Swing the view right">↻</button></span><span class="sp"></span><button data-a="fit" title="Back to fit">fit</button></div>';
        this._r = {}; this.querySelectorAll('[data-r]').forEach((el) => { this._r[el.dataset.r] = el; });
        this.addEventListener('click', (e) => this._click(e));
        /* the relation edges light under the pointer (defect 83). The scene's own runs are untouched; these are the
           `.rel` segments, each carrying the two records it joins. In Full they are drawn anyway, so this only
           shows itself in Hover and Zen - but the marking is the same in every tier, so nothing special-cases. */
        this.addEventListener('mouseover', (e) => { const t = e.target && e.target.closest && e.target.closest('[data-id]'); this._relHot(t ? t.dataset.id : ''); });
        this.addEventListener('mouseleave', () => this._relHot(''));
        // the timeline: the slider and ← → walk the session's turns
        if (!this.hasAttribute('tabindex')) this.setAttribute('tabindex', '0');
        if (this._r.scrub) this._r.scrub.addEventListener('input', () => { const ts = this._S.scene.turns || []; const t = ts[Math.max(0, Math.min(ts.length - 1, +this._r.scrub.value || 0))]; if (t && t.mid !== this._S.scene.sel) this.select(t.mid); });
        this.addEventListener('keydown', (ev) => { if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return; if (ev.target && /^(input|textarea)$/i.test(ev.target.tagName)) return; const ts = this._S.scene.turns || []; if (!ts.length) return; let i = ts.findIndex((t) => t.mid === this._S.scene.sel); i = Math.max(0, Math.min(ts.length - 1, i + (ev.key === 'ArrowRight' ? 1 : -1))); this.select(ts[i].mid); ev.preventDefault(); });
        const wrap = this._r.wrap;
        // the wheel zooms the whole scene about the pointer: plates, runs, cards, widgets and nodes grow together
        // ctrl (or Command, or a pinch) zooms the scene; a plain wheel scrolls the page past it (defect 75)
      wrap.addEventListener('wheel', (e) => { if (this._S.mode === 'cards') return; if (!(e.ctrlKey || e.metaKey)) return; e.preventDefault(); const p = this._S.pan; const nz = Math.max(0.4, Math.min(3, p.z * (e.deltaY > 0 ? 0.9 : 1.12))); const r = wrap.getBoundingClientRect(); const qx = e.clientX - (r.left + r.width / 2), qy = e.clientY - (r.top + r.height / 2); const k = nz / p.z; this._S.pan = { z: nz, x: qx - (qx - p.x) * k, y: qy - (qy - p.y) * k }; this._applyPan(); }, { passive: false });
        /* PICK UP AND DROP: a widget on the canvas plane (data-drag) follows the pointer once it has moved a little; the plate
           under the pointer lights as the drop target, an item under it says the widget would land before it; the drop is
           reported to the host (vera:xpl:move), which re-anchors the record — the scene redraws from the host's answer.
           The click that would follow is swallowed, so a drop never opens the card. */
        this.addEventListener('pointerdown', (e) => { if (e.button) return; const it = e.target.closest && e.target.closest('[data-drag]'); if (!it || (e.target.closest && e.target.closest('button'))) return;
          this._dragW = { id: it.dataset.id, key: it.dataset.key || '', from: it.dataset.mid || '', el: it, x0: e.clientX, y0: e.clientY, pid: e.pointerId, on: false, to: '', before: '' }; });
        /* a screen point into the stage's own coordinates: the view is scaled by the pan zoom, and the whole chat may sit
           under a CSS zoom in the harness — the view's box against its untransformed size is the one true scale, so the
           lifted item stays under the pointer whatever the zoom (cards: the stage scrolls and scales inside the view) */
        const local = (cx, cy) => { const v = this._r.view, r = v.getBoundingClientRect(); const zoom = r.width / Math.max(1, v.offsetWidth || r.width);
          if (this._S.mode === 'cards') { const k = this._S.pan.z || 1; return { x: ((cx - r.left) / zoom + v.scrollLeft) / k, y: ((cy - r.top) / zoom + v.scrollTop) / k }; }
          return { x: (cx - r.left) / zoom, y: (cy - r.top) / zoom }; };
        this._dragLocal = local;
        this.addEventListener('pointermove', (e) => { const g = this._dragW; if (!g) return; const dx = e.clientX - g.x0, dy = e.clientY - g.y0;
          if (!g.on) { if (Math.abs(dx) + Math.abs(dy) < 6) return; g.on = true; g.el.classList.add('xp-lift'); wrap.classList.add('dropping'); g.l0 = local(g.x0, g.y0); try { this.setPointerCapture(g.pid); } catch (_) {} }
          const l = local(e.clientX, e.clientY); g.el.style.translate = (l.x - g.l0.x).toFixed(1) + 'px ' + (l.y - g.l0.y).toFixed(1) + 'px';   /* the translate property composes BEFORE the class transform, so the item's own foot-anchoring (translate + the fit's counter-scale) stays under the lift */
          const hit = this._dropAt(l.x, l.y, g.id); const pl = hit.mid ? this.querySelector('.xp-pl[data-mid="' + hit.mid + '"]') : null; const before = hit.before ? this.querySelector('[data-drag][data-id="' + hit.before + '"]') : null;
          this.querySelectorAll('.xp-pl.drop').forEach((el) => { if (el !== pl) el.classList.remove('drop'); }); this.querySelectorAll('.xp-drop-before').forEach((el) => { if (el !== before) el.classList.remove('xp-drop-before'); });
          if (pl) pl.classList.add('drop'); if (before) before.classList.add('xp-drop-before'); g.to = hit.mid || ''; g.before = hit.before || ''; g.at = hit.at || null; g.layer = hit.layer || ''; });
        const dropEnd = (e) => { const g = this._dragW; if (!g) return; this._dragW = null; if (!g.on) return; g.el.classList.remove('xp-lift'); g.el.style.translate = ''; wrap.classList.remove('dropping');
          this.querySelectorAll('.xp-pl.drop,.xp-drop-before').forEach((el) => { el.classList.remove('drop'); el.classList.remove('xp-drop-before'); }); try { this.releasePointerCapture(g.pid); } catch (_) {}
          this._dragJust = true; setTimeout(() => { this._dragJust = false; }, 250);
          const held = this._renderHeld; this._renderHeld = false;
          if (e.type === 'pointercancel' || !(g.to || g.before)) { this._schedule(); return; }
          if (held) this._schedule();
          const wg = this._itemOf(g.id); this.dispatchEvent(new CustomEvent('vera:xpl:move', { detail: { id: g.id, key: g.key || (wg && wg.key) || '', from: g.from, to: g.to || g.from, before: g.before, at: g.at, layer: g.layer, mode: this._S.mode, card: wg ? wg.card : null }, bubbles: true })); };
        this.addEventListener('pointerup', dropEnd); this.addEventListener('pointercancel', dropEnd);
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
      stack(on) { this._S.stack = on == null ? !this._S.stack : !!on; if (this._S.stack) this._S.solo = false; this._S.pan = { x: 0, y: 0, z: 1 }; this._schedule(); this._isoBring(); return this._S.stack; }   // stacking lands on the turn in focus, not wherever the deck happens to start
      // the board's single-turn iso: only the selected turn's plate (Stack and Turn are the board's `stacked` and `turn`)
      solo(on) { this._S.solo = on == null ? !this._S.solo : !!on; if (this._S.solo) this._S.stack = false; this._S.pan = { x: 0, y: 0, z: 1 }; this._schedule(); return this._S.solo; }
      // PTZ: the pan is the view's transform (a drag does the same); the tilt and the swing change the projection every plate,
      // band, stem, node and widget group is drawn through — the scene re-fits to the frame at the new angles
      pan(dx, dy) { const p = this._S.pan; if (this._S.mode === 'cards') { const v = this._r.view; if (v) v.scrollBy({ left: -dx, top: -dy, behavior: 'smooth' }); return; } p.x += dx; p.y += dy; this._applyPan(); }
      tilt(deg) { this._S.tilt = Math.max(12, Math.min(60, deg == null ? 30 : +deg)); this._schedule(); this._isoCentre(); return this._S.tilt; }
      swing(deg) { this._S.azim = Math.max(25, Math.min(65, deg == null ? 45 : +deg)); this._schedule(); this._isoCentre(); return this._S.azim; }
      /* A rotation turns the scene about its own centre. tilt() and swing() re-project every plate and re-render, and
         used to leave the pan exactly where it was - so with an offset held (the stack selector holds one, and so does
         a drag) the deck swung out of the frame instead of turning on the spot (Notes/42 defect 80). After the
         re-projection the plate in focus, or the whole deck when nothing is picked, goes back under the middle of the
         frame at whatever zoom is held. */
      /* Light the relation edges that touch one record. The graph nodes (.xnd) carry the record's own id, which is
         exactly what a relation names at each end, so the match is direct. A card's id is "<mid>:<layer>:<i>" and
         names no relation - pointing at one simply lights nothing, which is right: the relations are between the
         records, not between the cards the turn made from them. */
      _relHot(id) {
        try {
          const key = String(id || ''); if (key === this._relLit) return; this._relLit = key;
          this.querySelectorAll('.xp-e.rel').forEach((e) => {
            e.classList.toggle('hot', !!key && (e.getAttribute('data-a') === key || e.getAttribute('data-b') === key));
          });
        } catch (_) {}
      }
      _isoCentre() {
        if (this._S.mode !== 'iso') return;
        const raf = root.requestAnimationFrame || ((f) => setTimeout(f, 16));
        raf(() => raf(() => { try {
          const sr = this.shadowRoot || this, wrap = this._r && this._r.wrap; if (!wrap) return;
          const mid = (this._S.scene || {}).sel, pls = [...sr.querySelectorAll('.xp-pl')];
          const pick = (mid && pls.find((q) => q.dataset.mid === mid)) || null;
          let cx, cy;
          if (pick) { const r = pick.getBoundingClientRect(); if (!(r.height > 0)) return; cx = r.left + r.width / 2; cy = r.top + r.height / 2; }
          else {
            let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
            pls.forEach((q) => { const r = q.getBoundingClientRect(); if (!(r.width > 0)) return; x0 = Math.min(x0, r.left); y0 = Math.min(y0, r.top); x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom); });
            if (!(x1 > x0)) return; cx = (x0 + x1) / 2; cy = (y0 + y1) / 2;
          }
          const b = wrap.getBoundingClientRect(); const p = this._S.pan;
          p.x += (b.left + b.width / 2) - cx; p.y += (b.top + b.height / 2) - cy; p.auto = false;
          this._applyPan();
        } catch (_) {} }));
      }
      /* what is under a stage point while a widget is in the hand, from the layout's own geometry (the plates are
         pointer-events:none and clipped polygons, so a DOM hit-test cannot answer): the plate (turn), the band or plate
         column it is over, the canvas item it would land before, and — inside the canvas band — the ground point the
         item will stand on, relative to the band (iso: through the inverse of the projection at the plate's floor) */
      _dropAt(x, y, selfId) {
        const o = this._last || {}; const out = { mid: '', layer: '', before: '', at: null };
        const inPoly = (p, poly) => { let ok = false; for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) { const a = poly[i], b = poly[j]; if ((a.y > p.y) !== (b.y > p.y) && p.x < (b.x - a.x) * (p.y - a.y) / (b.y - a.y) + a.x) ok = !ok; } return ok; };
        if (o.mode === 'iso') { const pl = (o.plates || []).find((q) => inPoly({ x, y }, q.poly)); if (!pl) return out; out.mid = pl.mid;
          const band = (o.bands || []).find((b) => b.si === pl.si && inPoly({ x, y }, b.poly)); out.layer = band ? band.layer : '';
          const landB = (band && band.layer === 'land') ? band : (o.bands || []).find((b) => b.si === pl.si && b.layer === 'land');   /* dropped on another band of the plate: the widget still lands on the canvas, in the pointer's column */
          if (landB && o.ground) { const gr = o.ground, px0 = (x - gr.dx) / gr.s, py0 = (y - gr.dy) / gr.s; const T = gr.tilt * Math.PI / 180, A = gr.azim * Math.PI / 180, sT = Math.sin(T), cT = Math.cos(T), cA = Math.cos(A), sA = Math.sin(A);
            const q = (py0 + (pl.z || 0) * cT) / Math.max(0.05, sT); const u = px0 * cA + q * sA, v = -px0 * sA + q * cA; out.at = { iso: { u: Math.round(u - (landB.u0 != null ? landB.u0 : pl.u0)), v: Math.round(v - landB.v0) } }; }
          const near = (o.widgets || []).filter((w) => w.layer === 'land' && w.mid === pl.mid && w.id !== selfId).map((w) => ({ w, d: Math.hypot(w.x - x, w.y - y) })).sort((a, b) => a.d - b.d)[0]; if (near && near.d < 70) out.before = near.w.id; return out; }
        const pl = (o.plates || []).find((q) => x >= q.x && x <= q.x + q.w && y >= q.y && y <= q.y + q.h); if (!pl) return out; out.mid = pl.mid; out.layer = pl.layer || '';
        if (pl.layer === 'land') { out.at = { cards: { x: Math.round(x - pl.x), y: Math.round(y - pl.y) } }; const near = (o.cards || []).filter((c) => c.layer === 'land' && c.mid === pl.mid && c.id !== selfId && x >= c.x - 10 && x <= c.x + c.w + 10 && y >= c.y - 6 && y <= c.y + c.h + 6)[0]; if (near) out.before = near.id; }
        return out;
      }
      // the laid-out item behind a DOM id (a card or an iso widget), for the events that name one
      _itemOf(id) { const o = this._last || {}; return (o.widgets || []).find((w) => w.id === id) || (o.cards || []).find((c) => c.id === id) || null; }
      widgetSize(s) { const L = ['s', 'm', 'l']; this._S.wsz = L.includes(s) ? s : L[(L.indexOf(this._S.wsz || 'm') + 1) % L.length]; this._schedule(); return this._S.wsz; }
      mode(name) { if (name && /^(cards|front|iso)$/.test(name)) { this._S.mode = name; this._S.pan = { x: 0, y: 0, z: 1, auto: true }; this._S.open = null; this._S.focus = null; this._frontKey = null; this._schedule(); } return this._S.mode; }
      select(mid) { this._S.scene.sel = mid; this._schedule(); this._isoBring(); this.dispatchEvent(new CustomEvent('vera:xpl:turn', { detail: { mid }, bubbles: true })); }
      /* Stacked, the deck is taller than the frame as soon as a session has a few turns — with eight turns four of the
         eight plates are off it — so a turn picked in the selector was lit where it could not be seen and nothing moved
         (Notes/42 defect 62). The plate picked is brought into the frame. Turn draws the picked turn by itself, so it
         needs none of this, and the other modes are left alone. */
      _isoBring() {
        if (this._S.mode !== 'iso' || !(this._S.stack || (this._last && this._last.stack))) return;
        const go = () => { try {
          const sr = this.shadowRoot || this, mid = (this._S.scene || {}).sel; if (!mid) return;
          const pl = [...sr.querySelectorAll('.xp-pl')].find((q) => q.dataset.mid === mid), wrap = this._r && this._r.wrap;
          if (!pl || !wrap) return;
          const r = pl.getBoundingClientRect(), b = wrap.getBoundingClientRect(); if (!(r.height > 0 && b.height > 0)) return;
          if (r.left >= b.left - 2 && r.right <= b.right + 2 && r.top >= b.top - 2 && r.bottom <= b.bottom + 2) return;   // already in the frame: the view stays where it is
          const p = this._S.pan; p.x += (b.left + b.width / 2) - (r.left + r.width / 2); p.y += (b.top + b.height / 2) - (r.top + r.height / 2); p.auto = false;
          this._applyPan();
        } catch (_) {} };
        const raf = root.requestAnimationFrame || ((f) => setTimeout(f, 16));
        raf(() => raf(go));   // after the render this scheduled
      }
      fit() { this._S.pan = { x: 0, y: 0, z: 1, auto: true }; this._applyPan(); this._schedule(); }   // back to the whole scene: the front takes its fit again
      state() { return this._S; }
      // the context window's meter for the chip bar (the host's ctx_used / ctx_max; the scene may carry it as scene.budget)
      setBudget(used, max) { this._S.budget = (max > 0) ? { used: +used || 0, max: +max } : null; this._schedule(); }
      focus(li) { this._S.focus = li == null || this._S.focus === li ? null : li; if (li != null) this._S.layer = li; this._schedule(); return this._S.focus; }
      // the pan zoom is one transform on the view: nothing counter-scales against it (the items' --inv follows the fit alone)
      _applyPan() { const p = this._S.pan; if (this._r.view) this._r.view.style.transform = this._S.mode === 'cards' ? 'none' : 'translate(' + p.x + 'px,' + p.y + 'px) scale(' + p.z + ')'; if (this._r.zoom) this._r.zoom.textContent = Math.round(p.z * 100) + '%';
        // cards: the stage scales about its corner and the wrap's inner box follows, so the scroll range is the zoomed scene's
        if (this._S.mode === 'cards' && this._r.view) { const st = this._r.view.querySelector('.xp-stage'), w = this._r.view.querySelector('.xp-stage-w'); if (st && w) { st.style.transform = 'scale(' + p.z + ')'; w.style.width = Math.round(parseFloat(st.style.width) * p.z) + 'px'; w.style.height = Math.round(parseFloat(st.style.height) * p.z) + 'px'; } } }
      _click(e) {
        if (this._dragJust) { this._dragJust = false; return; }   // a drop is not a click
        const t = e.target;
        const eb = t.closest && t.closest('[data-edit]'); if (eb) { const it = eb.closest('[data-id]'); const id = it ? it.dataset.id : ''; const [mid] = id.split(':'); const wg = this._itemOf(id); e.stopPropagation(); this.dispatchEvent(new CustomEvent('vera:xpl:edit', { detail: { id, mid, key: (it && it.dataset.key) || (wg && wg.key) || '', card: wg ? wg.card : null }, bubbles: true })); return; }
        const mb = t.closest && t.closest('button[data-m]'); if (mb) { this.mode(mb.dataset.m); return; }
        const ab = t.closest && t.closest('[data-a]'); if (ab) { const k = ab.dataset.a; if (k === 'fit') this.fit(); else if (k === 'stack') this.stack(); else if (k === 'wsz') this.widgetSize(); else if (k === 'place') { const ts = this._S.scene.turns || []; const t = ts.find((x) => x.mid === this._S.scene.sel) || ts[ts.length - 1]; this.dispatchEvent(new CustomEvent('vera:xpl:place', { detail: { mid: t ? t.mid : '' }, bubbles: true })); } else if (k === 'close') this.dispatchEvent(new CustomEvent('vera:xpl:close', { bubbles: true })); else if (k === 'solo') this.solo(true); else if (k === 'all') { this._S.solo = false; this._S.stack = false; this._S.pan = { x: 0, y: 0, z: 1 }; this._schedule(); } else if (k === 'panl' || k === 'panr' || k === 'panu' || k === 'pand') this.pan(k === 'panl' ? 80 : k === 'panr' ? -80 : 0, k === 'panu' ? 80 : k === 'pand' ? -80 : 0); else if (k === 'tiltu' || k === 'tiltd') this.tilt(this._S.tilt + (k === 'tiltu' ? 6 : -6)); else if (k === 'swl' || k === 'swr') this.swing(this._S.azim + (k === 'swl' ? -5 : 5)); else if (k === 'zin' || k === 'zout') { const p = this._S.pan; p.auto = false; p.z = Math.max(0.4, Math.min(3, p.z * (k === 'zin' ? 1.2 : 0.83))); this._applyPan(); } else if (k === 'prev' || k === 'next') { this._S.layer = Math.max(0, Math.min(LAYERS.length - 1, this._S.layer + (k === 'next' ? 1 : -1))); this._schedule(); } return; }
        const cb = t.closest && t.closest('.xp-cb'); if (cb) { if (cb.dataset.layer) { const L = Object.assign({}, this._S.layers); L[cb.dataset.layer] = !L[cb.dataset.layer]; this.layers(L); return; } if (cb.dataset.galaxy) { this.galaxy(); return; } if (cb.dataset.rel) this._S.related = !this._S.related; else if (cb.dataset.lane) { this._S.lanesOff = Object.assign({}, this._S.lanesOff); this._S.lanesOff[cb.dataset.lane] = !this._S.lanesOff[cb.dataset.lane]; } this._schedule(); return; }
        const dot = t.closest && t.closest('.xp-dot'); if (dot) { this.select(dot.dataset.mid); return; }
        const st = t.closest && t.closest('.xp-lb.station'); if (st) { this.select(st.dataset.mid); return; }
        const ph = t.closest && t.closest('.xp-cp-h'); if (ph) { this.focus(+ph.closest('.xp-cp').dataset.li); return; }   // a layer's header focuses the whole section; again puts it back
        const hit = t.closest && t.closest('.xp-lb.hit'); if (hit) { const v = this._r.view, x = parseFloat(hit.style.left) || 0; if (v && v.classList.contains('scroll')) v.scrollTo({ left: Math.max(0, x - 40), behavior: 'smooth' }); return; }   // a station caption frames its own column
        const card = t.closest && t.closest('.xp-it,.xp-rc,.xit,.xig,.xnd'); if (card) { const id = card.dataset.id; this._S.open = this._S.open === id ? null : id; const [mid, layer] = id.split(':'); this._schedule(); this.dispatchEvent(new CustomEvent('vera:xpl:pick', { detail: { mid, layer, card: id }, bubbles: true })); if (layer === 'say') this.dispatchEvent(new CustomEvent('vera:xpl:turn', { detail: { mid }, bubbles: true })); return; }
        const cp = t.closest && t.closest('.xp-cp'); if (cp) { this._S.layer = +cp.dataset.li; this._schedule(); }
      }
      /* ── FRONT: the carousel is built once per station and then driven IN PLACE — a layer change moves the panels by
         their CSS transition (they glide; nothing is rebuilt, so nothing swings in again), the cards past the window are
         dropped and given back as the window moves, and the runs are re-laid over the measured cards, held back by their
         keyframe until the panels land. A new station (or a new room) rebuilds, and the panels swing in from flat. ── */
      _renderFront(o) {
        const S = this._S, view = this._r.view, P = o.panels; const PW = o.panel ? o.panel.w : 400, PH = o.panel ? o.panel.h : 472;
        const rcFace = (c) => (c.card && (c.card.form || c.card.record || String(c.card.kind || '').toLowerCase() === 'widget') ? faceHtml(c.card, widgetOf(c.card), S.wsz) : '');   // a widget's own face on the carousel card
        const rcHtml = (c) => '<div class="xp-rc' + (S.open === c.id ? ' open' : '') + (String(c.card.kind || '') === 'gen' ? ' gen' : '') + '" data-id="' + esc(c.id) + '" data-ci="' + c.ci + '" style="--cc:' + esc(c.col) + '" title="' + esc(c.card.n || '') + (c.card.d ? ' — ' + esc(c.card.d) : '') + ' · click for the record"><span class="n">' + (c.card.tpl ? '<i class="tpl" title="placed from the registry · ' + esc(c.card.tpl) + '">⧉</i> ' : '') + esc(c.card.n || '') + '</span><span class="d">' + esc(c.card.d || '') + '</span>' + rcFace(c) + '<div class="b">' + cardBody(c.card) + '</div></div>';
        const bodyHtml = (p) => (p.graph && p.graph.nodes.length ? '<div class="xp-gp">' + graphHtml(p.graph, 200) + '</div>' : '') + p.cards.map(rcHtml).join('') + (p.cards.length || (p.graph && p.graph.nodes.length) ? '' : '<div class="d" style="color:var(--xp-t3);font-family:var(--xp-mono);font-size:9px">nothing here for this turn</div>');
        const key = (o.station ? o.station.mid : '') + '|' + P.map((p) => p.n).join(',') + '|' + PW + 'x' + PH;
        let car = view.querySelector('.xp-car');
        if (!car || this._frontKey !== key) {
          this._frontKey = key;
          view.innerHTML = '<div class="xp-car"><div class="xp-track"><span class="xp-spine"><i></i></span>' + o.leaders.map((l, i) => '<div class="xp-lead ' + l.cls + '" data-i="' + i + '" style="transform:' + l.tf + ';width:' + l.w + '"></div>').join('') + '<div class="xp-runs" data-r="runs"></div>'
            + P.map((p) => '<div class="xp-cp ' + p.cls + (p.gen ? ' gen' : '') + '" data-li="' + p.li + '" style="--pc:' + esc(p.col) + ';--d:' + p.d + ';--el:' + p.el + ';left:' + (-PW / 2) + 'px;top:' + (-PH / 2) + 'px;width:' + PW + 'px;height:' + PH + 'px;transform:' + p.tf + '"><div class="xp-cp-h"><i></i>' + esc(p.name) + '<span class="sub"> · ' + esc(p.sub) + '</span><b>' + p.n + '</b><span class="fx2">' + p.focL + '</span></div><div class="xp-cp-b" data-cards="' + esc(p.cards.map((c) => c.id + ':' + (c.card.kind || '') + ':' + String(c.card.n || '').length).join(',')) + '">' + bodyHtml(p) + '</div></div>').join('')
            + '</div><button class="xp-nav l" data-a="prev" title="Previous layer">‹</button><button class="xp-nav r" data-a="next" title="Next layer">›</button></div>';
          car = view.querySelector('.xp-car'); if (view.innerHTML.indexOf('<vera-mermaid') >= 0) ensureMermaid(this.ownerDocument);
        } else {
          P.forEach((p) => { const el = car.querySelector('.xp-cp[data-li="' + p.li + '"]'); if (!el) return; el.className = 'xp-cp ' + p.cls + (p.gen ? ' gen' : ''); el.style.transform = p.tf;
            const hb = el.querySelector('.xp-cp-h b'); if (hb) hb.textContent = p.n; const fx = el.querySelector('.xp-cp-h .fx2'); if (fx) fx.textContent = p.focL;
            const body = el.querySelector('.xp-cp-b'), want = p.cards.map((c) => c.id + ':' + (c.card.kind || '') + ':' + String(c.card.n || '').length).join(','); if (body && body.dataset.cards !== want) { body.innerHTML = bodyHtml(p); body.dataset.cards = want; } });
          o.leaders.forEach((l, i) => { const el = car.querySelector('.xp-lead[data-i="' + i + '"]'); if (el) { el.style.transform = l.tf; el.style.width = l.w; } });
          car.querySelectorAll('.xp-rc[data-id]').forEach((el) => el.classList.toggle('open', S.open === el.dataset.id));
        }
        // the runs: the inset first (it sizes the cards, so it goes before they are measured), then each card's centre in
        // its panel — offsetTop is the panel's own coordinate, untouched by the 3D transform — and the segments over them
        const track = car.querySelector('.xp-track'), runs = car.querySelector('[data-r="runs"]');
        const pad = frontRuns(o, null).pad; track.style.setProperty('--cpad', pad + 'px');
        const yOf = (li, ci) => { const el = car.querySelector('.xp-cp[data-li="' + li + '"] .xp-rc[data-ci="' + ci + '"]'); if (!el) return null; const body = el.closest('.xp-cp-b'); return el.offsetTop - (body ? body.scrollTop : 0) + el.offsetHeight / 2 - PH / 2; };
        const R = frontRuns(o, yOf); this._frontRuns = R;
        runs.innerHTML = R.h.map((r) => '<span class="xp-cr ' + esc(r.cls) + '" title="' + esc(r.title) + '" style="transform:' + r.tf + ';width:' + r.w + ';border-top-color:' + esc(r.col) + ';animation-name:' + r.anim + '"></span>').join('')
          + R.v.map((r) => '<span class="xp-crv ' + esc(r.cls) + '" title="' + esc(r.title) + '" style="transform:' + r.tf + ';height:' + r.h + ';background:' + esc(r.col) + ';animation-name:' + r.anim + '"></span>').join('')
          + R.z.map((r) => '<span class="xp-crz ' + esc(r.cls) + '" title="' + esc(r.title) + '" style="transform:' + r.tf + ';height:' + r.h + ';background:' + esc(r.col) + ';animation-name:' + r.anim + '"></span>').join('');
        if (S.pan.auto) S.pan.z = o.fitZ || 1;   // the board's fit for the room, until the user zooms
        this._applyPan(); this._emit(o);
      }
      _schedule() { if (this._raf || !this._built) return; this._raf = (root.requestAnimationFrame || setTimeout)(() => { this._raf = 0; this._render(); }); }
      _render() {
        if (this._dragW && this._dragW.on) { this._renderHeld = true; return; }   // a widget is in the hand: nothing is rebuilt under it until the drop
        const S = this._S, wrap = this._r.wrap, view = this._r.view; const W = wrap.clientWidth || 800, H = wrap.clientHeight || 600;
        const ISO = root.VeraISO && typeof root.VeraISO.proj === 'function' ? root.VeraISO : null;
        const proj = ISO ? root.VeraISO.proj(S.tilt || 30, S.azim || 45, 1, true) : null;   // the shared projection when it is there, at the view's tilt and swing; z in px, as the floors are measured
        const den = (this.ownerDocument && this.ownerDocument.documentElement.getAttribute('data-den') || 'full').toLowerCase(); this.dataset.den = den;
        const o = layout(S.scene, S.mode, W, H, { layer: S.layer, proj, den, stack: S.stack, solo: S.solo, tilt: S.tilt, azim: S.azim, wsz: S.wsz, focus: S.focus, heights: S.heights, related: S.related, lanesOff: S.lanesOff, layers: S.layers, galaxy: S.galaxy }); this._last = o;
        // the chip bar: the selected turn's layers with their counts (each a toggle), related, the window's meter — over the stage in cards and iso
        if (this._r.ctx) { const bud = S.budget || (S.scene && S.scene.budget) || null, kf = (n) => n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
          const bar = (o.ctx && o.mode !== 'front' && (S.scene.turns || []).length) ? '<span class="c">context</span>' + o.ctx.lanes.map((l) => '<button class="xp-cb' + (l.off ? '' : ' on') + '" data-lane="' + esc(l.lane) + '" style="--lc:' + esc(l.col) + '" title="' + l.n + ' ' + esc(l.lane) + ' record' + (l.n === 1 ? '' : 's') + ' in this turn\u2019s context \u2014 click to hide or show the lane"><i></i>' + esc(l.lane) + '<b>' + l.n + '</b></button>').join('')
            + '<button class="xp-cb' + (o.ctx.related ? ' on' : '') + '" data-rel="1" title="Show the entries that relate but were not injected">related<b>' + o.ctx.ghosts + '</b></button>'
            + '<span class="sep"></span><span class="c">layers</span><button class="xp-cb' + (S.layers.activity ? ' on' : '') + '" data-layer="activity" style="--lc:var(--xp-ac2)" title="Activity \u2014 the capability calls each turn made, hung off the card that triggered them, in the order they ran"><i></i>activity' + (o.ctx.acts ? '<b>' + o.ctx.acts + '</b>' : '') + '</button>'
            + '<button class="xp-cb' + (S.layers.estate ? ' on' : '') + '" data-layer="estate" style="--lc:var(--xp-dv3)" title="Estate \u2014 where the calls ran: the subsystems and the machines behind them"><i></i>estate' + (o.ctx.ests ? '<b>' + o.ctx.ests + '</b>' : '') + '</button>'
            + '<button class="xp-cb' + (S.galaxy ? ' on' : '') + '" data-galaxy="1" style="--lc:var(--xp-dv1)" title="The mini context galaxy on the graph band \u2014 the board draws the lanes alone"><i></i>galaxy</button>'
            + (bud && bud.max ? '<span class="xp-cbb" title="the context window: used / max">' + kf(bud.used) + ' / ' + kf(bud.max) + '<i style="width:' + Math.min(100, Math.round(bud.used / bud.max * 100)) + '%"></i></span>' : '') : '';
          this._r.ctx.innerHTML = bar; this._r.ctx.style.display = bar ? '' : 'none'; }
        this.querySelectorAll('.xp-ctl button[data-a="stack"]').forEach((b) => { b.classList.toggle('on', !!S.stack && o.mode === 'iso'); b.style.display = o.mode === 'iso' ? '' : 'none'; });
        this.querySelectorAll('.xp-ctl button[data-a="solo"]').forEach((b) => { b.classList.toggle('on', !!S.solo && !S.stack && o.mode === 'iso'); b.style.display = o.mode === 'iso' ? '' : 'none'; });
        this.querySelectorAll('.xp-ctl button[data-a="all"]').forEach((b) => { b.classList.toggle('on', !S.solo && !S.stack && o.mode === 'iso'); b.style.display = o.mode === 'iso' ? '' : 'none'; });
        if (this._r.isoc) this._r.isoc.style.display = o.mode === 'iso' ? '' : 'none';
        if (this._r.tl) this._r.tl.style.display = o.mode === 'iso' ? '' : 'none'; if (this._r.ang) this._r.ang.textContent = Math.round(S.tilt || 30) + '° · ' + Math.round(S.azim || 45) + '°';
        this.querySelectorAll('.xp-ctl button[data-a="wsz"]').forEach((b) => { b.textContent = (S.wsz || 'm').toUpperCase(); b.style.display = o.mode === 'iso' ? '' : 'none'; });
        if (this._r.scrub) { this._r.scrub.max = String(Math.max(0, (S.scene.turns || []).length - 1)); this._r.scrub.value = String(o.sel); }
        this.querySelectorAll('.xp-ctl button[data-m]').forEach((b) => b.classList.toggle('on', b.dataset.m === o.mode));
        this._r.dots.innerHTML = (S.scene.turns || []).map((t, i) => '<div class="xp-dot' + (i === o.sel ? ' on' : '') + '" data-mid="' + esc(t.mid) + '" title="' + esc((t.who || 'you') + ' · ' + (t.t || '') + ' · ' + String(t.text || '').slice(0, 80)) + '"><span class="l">' + esc((t.who || 'you') + ' ' + (i + 1) + ' · ' + String(t.text || '').slice(0, 30)) + '</span><i></i></div>').join('');
        view.classList.toggle('scroll', o.mode === 'cards'); wrap.classList.toggle('cards', o.mode === 'cards');
        view.style.setProperty('--inv', o.mode === 'iso' ? String(o.inv || 1) : '1');   // the fit's counter-scale, once per render; the zoom never touches it
        const st = (x, y) => 'left:' + x + 'px;top:' + y + 'px;';
        const itHtml = (c, anch) => '<div class="xp-it' + (anch ? ' anch' : '') + (c.card && c.card.src ? ' has-img' : '') + (S.open === c.id ? ' open' : '') + '" data-id="' + esc(c.id) + '" title="' + esc(c.card.n || '') + (c.card.d ? ' — ' + esc(c.card.d) : '') + '" style="' + st(c.x, c.y) + 'width:' + c.w + 'px;height:' + c.h + 'px;--cc:' + esc(c.col) + '"><span class="n">' + (c.card.tpl ? '<i class="tpl" title="placed from the registry · ' + esc(c.card.tpl) + '">⧉</i> ' : '') + esc(c.card.n || '') + '</span><span class="d">' + esc(c.card.d || '') + '</span><div class="b">' + cardBody(c.card) + '</div></div>';
        let h = '';
        if (!(S.scene.turns || []).length) { view.innerHTML = '<div class="xp-empty">Nothing to explode yet — the scene is the session\'s turns: what each read, what it said, what it made, where it landed.</div>'; view.style.transform = 'none'; this._emit(o); return; }
        if (o.mode === 'front') { this._renderFront(o); return; }
        // ISO: the board's card on its stem; a widget as an iso widget group with its frameless caption; a context record as a typed node
        const tplTag = (c) => (c.tpl ? '<i class="tpl" title="placed from the registry · ' + esc(c.tpl) + '">⧉</i> ' : '');
        // a widget on the canvas plane can be picked up (dropped on another turn's plate, or before another item) and edited
        const dragAttr = (w) => (w.drag ? ' data-drag="1" data-mid="' + esc(w.mid) + '" data-key="' + esc(w.key || '') + '"' : '');
        const editBtn = (w) => (w.drag ? '<button class="xit-edit" data-edit="1" title="Edit this widget\u2019s record">\u2699</button>' : '');
        const xitHtml = (wg, face) => { const c = wg.card, open = S.open === wg.id; const wd = { form: wg.form, data: wg.data, sample: wg.sample }; const b = isoBody(c, face ? null : wd);   // the face says it all: no reading line beside it
          return '<div class="xit bb' + (wg.tight ? ' tight' : '') + (open ? ' open' : '') + (c.src ? ' has-img' : '') + (face ? ' face' : '') + (String(c.kind || '') === 'gen' ? ' gen' : '') + '" data-id="' + esc(wg.id) + '"' + dragAttr(wg) + ' title="' + esc(c.n || '') + (c.d ? ' — ' + esc(c.d) : '') + ' · click for the record" style="left:' + (wg.x - wg.cw / 2).toFixed(1) + 'px;top:' + (wg.y - wg.stem).toFixed(1) + 'px;width:' + wg.cw + 'px;--ih:' + wg.ch + 'px;--cc:' + esc(wg.col) + '">'
            + '<span class="xit-n">' + tplTag(c) + esc(c.n || '') + '</span><span class="xit-d">' + esc(c.d || '') + '</span>' + editBtn(wg) + (face || '')
            + (b.on ? '<span class="xit-body">' + b.on + '</span>' : '') + (c.src ? '<img class="xp-img" src="' + esc(c.src) + '" alt="" loading="lazy">' : '') + (b.x ? '<div class="xit-x">' + b.x + '</div>' : '') + '</div>'; };
        // a widget on the plate: ITS OWN FORM's face on the board's card (the widget element draws it — defect 37: the
        // record's form, not one object per shape); the iso group only when the element is not on the page
        /* a widget standing on the SESSION CANVAS plane is the thing itself with a small label box beneath it, never
           a card (Notes/42 defect 79). The plane is the point: the scene has four of them (read · the exchange ·
           produced · session canvas) and this was written as "any widget with a face", which took the turn's own read
           and produced widgets out of their cards too (defect 82). Off the canvas plane a widget is the board's card
           again. The face is drawn with proj:'iso' already, so on the plane it reads as an object. */
        const xigHtml = (wg) => { const c = wg.card, open = S.open === wg.id; const onPlane = wg.layer === 'land';
          const face = faceHtml(c, widgetOf(c), S.wsz, { plane: onPlane }); const pw = onPlane ? Math.round(planeSize(wg.form, S.wsz || 'm').w) : wg.cw;
          if (face && !onPlane) return xitHtml(wg, face);   // read · the exchange · produced: the board's card, as before
          const g = face ? null : groupOf(wg, ISO, { tilt: S.tilt || 30, azim: S.azim || 45 }); const b = isoBody(c, null);
          const cap = '<div class="xit frameless' + (open ? ' open' : '') + '" data-id="' + esc(wg.id) + '"' + dragAttr(wg) + ' title="' + esc(c.n || '') + (c.d ? ' — ' + esc(c.d) : '') + ' · click for the detail" style="left:' + (wg.x - wg.cw / 2).toFixed(1) + 'px;top:' + (wg.y + 6).toFixed(1) + 'px;width:' + wg.cw + 'px;--cc:' + esc(wg.col) + '"><span class="xit-n">' + tplTag(c) + esc(c.n || '') + '</span>' + (wg.value ? '<span class="xit-cv">' + esc(wg.value) + '</span>' : '') + '<span class="xit-d">' + esc(c.d || '') + '</span>' + editBtn(wg)
            + '<div class="xit-x"><b style="color:var(--xp-t1)">' + esc(c.n || '') + '</b><br><span style="font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3)">' + esc(wg.form) + (wg.sample ? ' · no reading yet' : wg.value ? ' · ' + esc(wg.value) : '') + (c.tpl ? ' · ⧉ ' + esc(c.tpl) : '') + '</span>' + (b.on || b.x ? '<div class="xit-body" style="display:flex">' + b.on + b.x + '</div>' : '') + '</div></div>';
          // the face, standing on the CANVAS plate: the label box below it is the same cap the built object gets
          if (face) return '<div class="xig xigf' + (wg.sample ? ' sample' : '') + (open ? ' open' : '') + '" data-id="' + esc(wg.id) + '"' + dragAttr(wg) + ' title="' + esc(c.n || '') + ' \u00b7 click for the detail" style="' + st(wg.x, wg.y) + '--xw:' + pw + 'px">' + face + '</div>' + cap;
          if (!g) return xitHtml(wg);   // no iso lib on the page: the widget is the board's flat widget card
          return '<div class="xig' + (wg.sample ? ' sample' : '') + (open ? ' open' : '') + '" data-id="' + esc(wg.id) + '"' + dragAttr(wg) + ' title="' + esc(c.n || '') + ' · click for the detail" style="' + st(wg.x, wg.y) + '--wsh:' + (-g.sh).toFixed(1) + 'px;--cc:' + esc(wg.col) + '">'
            + g.faces.map((f) => '<i class="xiw ' + f.k + (f.cls ? ' ' + f.cls : '') + '" style="left:' + f.x + ';top:' + f.y + ';width:' + f.w + ';height:' + f.h + ';--cp:' + f.cp + ';--fc:' + f.col + '"></i>').join('')
            + (g.needle ? '<span class="xiw-n" style="left:' + g.needle.x + ';top:' + g.needle.y + ';width:' + g.needle.len + ';transform:rotate(' + g.needle.deg + ')"></span>' : '')
            + (g.big ? '<span class="xiw-b" style="left:' + g.big.x + ';top:' + g.big.y + '">' + esc(g.big.n) + '</span>' : '') + '</div>' + cap; };
        // CARDS: the same card, top-anchored on its row line — name · meta · the body by kind · its chips; the record (the rest of the body, the layer, the turn) behind a click
        const ctHtml = (c) => { const card = c.card, open = S.open === c.id; const wd = widgetOf(card); const face = (card.form || card.record || String(card.kind || '').toLowerCase() === 'widget') ? faceHtml(card, wd, S.wsz) : ''; const b0 = isoBody(card, face ? null : wd); const relOn = card.score != null && /xf-score/.test(b0.on); const b = { on: relOn ? b0.on.replace(/<span class="xf-score">[\s\S]*?<\/b><\/span>/, '') : b0.on, x: b0.x };
          const rel = relOn ? '<span class="xf-rel"><i><b style="width:' + Math.round(Math.max(0, Math.min(1, +card.score)) * 100) + '%"></b></i>' + (/\d\.\d\d\s*$/.test(String(card.d || '')) ? '' : '<em>' + (+card.score).toFixed(2) + '</em>') + '</span>' : '';   // the number only when the meta does not already end with it
          return '<div class="xit ct' + (open ? ' open' : '') + (card.src ? ' has-img' : '') + (face ? ' face' : '') + (String(card.kind || '') === 'gen' ? ' gen' : '') + '" data-id="' + esc(c.id) + '"' + dragAttr(c) + ' title="' + esc(card.n || '') + (card.d ? ' — ' + esc(card.d) : '') + ' · click for the record" style="' + st(c.x, c.y) + 'width:' + c.w + 'px;--ih:' + c.ih + 'px;--cc:' + esc(c.col) + '">'
            + '<span class="xit-n">' + tplTag(card) + esc(card.n || '') + '</span><span class="xit-d">' + esc(card.d || '') + rel + '</span>' + editBtn(c) + face
            + (b.on ? '<span class="xit-body">' + b.on + '</span>' : '') + (card.src ? '<img class="xp-img" src="' + esc(card.src) + '" alt="" loading="lazy">' : '')
            + (c.badge ? '<span class="xit-m"><span class="xit-b">' + esc(c.badge) + '</span></span>' : '')
            + '<div class="xit-x">' + b.x + '<span class="xit-xr">layer<b>' + esc(c.layer) + '</b></span><span class="xit-xr">turn<b>' + esc(c.turn) + '</b></span>' + (card.kind ? '<span class="xit-xr">kind<b>' + esc(card.kind) + '</b></span>' : '') + '</div></div>'; };
        if (o.mode === 'iso') {
          this.classList.toggle('stacked', !!o.stack);
          o.plates.forEach((p) => { const xs = p.poly.map((q) => q.x), ys = p.poly.map((q) => q.y); const x0 = Math.min.apply(null, xs), y0 = Math.min.apply(null, ys), x1 = Math.max.apply(null, xs), y1 = Math.max.apply(null, ys); const cp = 'polygon(' + p.poly.map((q) => (q.x - x0).toFixed(1) + 'px ' + (q.y - y0).toFixed(1) + 'px').join(',') + ')'; h += '<div class="xp-pl iso' + (p.cls ? ' ' + p.cls : '') + '" data-mid="' + esc(p.mid || '') + '" style="' + st(x0, y0) + 'width:' + (x1 - x0).toFixed(1) + 'px;height:' + (y1 - y0).toFixed(1) + 'px;--cp:' + cp + '"></div>'; });
        } else { o.plates.forEach((p) => { h += '<div class="xp-pl' + (p.cls ? ' ' + p.cls : '') + '" data-mid="' + esc(p.mid || '') + '" style="' + st(p.x, p.y) + 'width:' + p.w + 'px;height:' + p.h + 'px;--pc:' + esc(p.col || 'var(--xp-ac)') + '"></div>'; }); }
        (o.bands || []).forEach((b) => { const xs = b.poly.map((q) => q.x), ys = b.poly.map((q) => q.y); const x0 = Math.min.apply(null, xs), y0 = Math.min.apply(null, ys), x1 = Math.max.apply(null, xs), y1 = Math.max.apply(null, ys); const cp = 'polygon(' + b.poly.map((q) => (q.x - x0).toFixed(1) + 'px ' + (q.y - y0).toFixed(1) + 'px').join(',') + ')'; h += '<div class="xp-band ' + esc(b.layer) + (b.empty ? ' empty' : '') + (b.cls ? ' ' + b.cls : '') + '" style="' + st(x0, y0) + 'width:' + (x1 - x0).toFixed(1) + 'px;height:' + (y1 - y0).toFixed(1) + 'px;--cp:' + cp + ';--bc:' + esc(b.col) + '"></div>'; });
        // the plates' edges as lines, the bands' hairlines (shown when Blocks is off): the planes stay planes without their fills
        (o.outline || []).forEach((e) => { h += '<span class="xp-pe' + (e.cls ? ' ' + e.cls : '') + '" style="' + st(e.x, e.y) + 'width:' + e.len + 'px;transform:rotate(' + e.deg + 'deg)"></span>'; });
        (o.boutline || []).forEach((e) => { h += '<span class="xp-be" style="' + st(e.x, e.y) + 'width:' + e.len + 'px;transform:rotate(' + e.deg + 'deg);--bc:' + esc(e.col) + '"></span>'; });
        o.edges.forEach((e) => { h += '<div class="xp-e ' + e.cls + '"' + (e.joins ? ' data-a="' + esc(e.joins[0]) + '" data-b="' + esc(e.joins[1]) + '"' : '') + ' title="' + esc(e.title) + '" style="' + st(e.x, e.y) + 'width:' + e.len + 'px;--ec:' + esc(e.col) + ';transform:rotate(' + e.deg + 'deg)"></div>'; });
        o.labels.forEach((l) => { h += '<div class="xp-lb ' + l.cls + '" data-mid="' + esc(l.mid || '') + '" data-st="' + (l.st == null ? '' : l.st) + '" style="' + st(l.x, l.y) + 'color:' + esc(l.col) + (o.mode === 'iso' ? ';transform:' + (/\bmore\b/.test(l.cls) ? 'translate(-100%,-50%) ' : /\bempty\b/.test(l.cls) ? 'translate(-50%,-50%) ' : /\blane\b/.test(l.cls) ? 'translate(-100%,-50%) ' : /\bprompt\b/.test(l.cls) ? 'translate(-50%,-100%) ' : '') + 'scale(var(--inv,1))' : '') + '">' + esc(l.n) + '<b>' + esc(l.k) + '</b></div>'; });
        o.cards.forEach((c) => { if (c.ct) h += ctHtml(c); else if (!c.iw) h += itHtml(c, !!c.anchored); });
        o.graphs.forEach((g) => { h += g.iso
          ? '<div class="xp-g iso" data-id="' + esc(g.id) + '" style="' + st(g.x, g.y) + 'width:' + g.w + 'px;height:' + g.h + 'px">' + (root.VeraWidget && typeof root.VeraWidget.draw === 'function' ? root.VeraWidget.draw('context_graph', { nodes: g.data.nodes, rels: g.data.rels }, 'm', { height: g.h, width: g.w, bare: true, view: 'iso', full: false, hubMeta: g.data.nodes.length + ' rec' }) : graphHtml(g.data, g.h)) + '</div>'
          : '<div class="xp-g" data-id="' + esc(g.id) + '" style="' + st(g.x, g.y) + 'width:' + g.w + 'px;height:' + g.h + 'px">' + graphHtml(g.data, g.h - 10) + '</div>'; });
        (o.gnodes || []).forEach((n) => { h += '<span class="xnd' + (n.lit ? ' lit' : '') + (n.ghost ? ' ghost' : '') + (S.open === n.id ? ' sel' : '') + '" data-id="' + esc(n.id) + '" title="' + esc(n.label + ' — ' + n.lane + ' · relevance ' + n.score.toFixed(2) + (n.ghost ? ' · related, not injected' : ' · in the prompt')) + '" style="' + st(n.x, n.y) + 'width:' + n.d + 'px;height:' + n.d + 'px;--nc:' + esc(n.col) + ';opacity:' + n.op + '"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"><path d="' + n.icon + '"></path></svg><span class="xnl">' + esc(n.label) + '<b>' + esc(n.lane + ' · ' + n.score.toFixed(2)) + '</b></span></span>'; });
        // the two layers' nodes: a call, an estate node — the same typed ring, its label and reading under the pointer
        (o.anodes || []).concat(o.enodes || []).forEach((n) => { h += '<span class="xnd ' + (n.lane === 'activity' ? 'act' : 'est') + (/run|call|start/i.test(n.status) ? ' run' : '') + (S.open === n.id ? ' sel' : '') + '" data-id="' + esc(n.id) + '" title="' + esc(n.label + (n.meta ? ' \u00b7 ' + n.meta : '')) + '" style="' + st(n.x, n.y) + 'width:' + n.d + 'px;height:' + n.d + 'px;--nc:' + esc(n.col) + '"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"><path d="' + n.icon + '"></path></svg><span class="xnl">' + esc(n.label) + '<b>' + esc(n.meta || n.lane) + '</b></span></span>'; });
        // the stems first, then every item in paint order — the lower on the screen, the later (it stands in front)
        (o.widgets || []).forEach((wg) => { h += '<span class="xstem" style="left:' + wg.x.toFixed(1) + 'px;top:' + (wg.y - (wg.draw === 'group' ? 0 : wg.stem)).toFixed(1) + 'px;height:' + (wg.draw === 'group' ? 0 : wg.stem) + 'px;--sc:' + esc(wg.col) + '"><i></i></span>'; });
        (o.widgets || []).slice().sort((a, b) => a.y - b.y).forEach((wg) => { h += wg.draw === 'group' ? xigHtml(wg) : xitHtml(wg); });
        if (o.mode === 'cards') { const k = S.pan.z || 1; h = '<div class="xp-stage-w" style="width:' + Math.round(o.size.w * k) + 'px;height:' + Math.round(o.size.h * k) + 'px;overflow:hidden"><div class="xp-stage" style="width:' + o.size.w + 'px;height:' + o.size.h + 'px;transform:scale(' + k + ')">' + h + '</div></div>'; }
        const sx = view.scrollLeft, sy = view.scrollTop; view.innerHTML = h; this._applyPan();   // the stage is rebuilt; where it was scrolled to is kept
        if (h.indexOf('<vera-mermaid') >= 0) ensureMermaid(this.ownerDocument);
        if (o.mode === 'cards' && (sx || sy)) { view.scrollLeft = sx; view.scrollTop = sy; }
        // CARDS: measure every card's height (a tall one pushes the rows under it down) and lay out once more when one
        // changed — twice at most per render, so a hover that grows a card can never chase itself; then, on a new
        // selection, scroll the stage to the selected turn's row
        if (o.mode === 'cards') { const hs = {}; let changed = false; view.querySelectorAll('.xit.ct[data-id]').forEach((el) => { const k = el.dataset.id, hh = el.offsetHeight; hs[k] = hh; if (!S.heights[k] || Math.abs(S.heights[k] - hh) > 1) changed = true; });
          if (changed && (this._measureN || 0) < 2) { this._measureN = (this._measureN || 0) + 1; S.heights = Object.assign({}, S.heights, hs); this._schedule(); return; }
          this._measureN = 0; if (this._rowSel !== o.sel + ':' + (o.station ? '' : S.scene.sel)) { this._rowSel = o.sel + ':' + S.scene.sel; const row = (o.rows || [])[o.sel]; if (row && (row.y < view.scrollTop || row.y + Math.min(row.h, view.clientHeight - 60) > view.scrollTop + view.clientHeight)) view.scrollTop = Math.max(0, (row.y - 92) * (S.pan.z || 1)); } }   // the row's caption lands under the chip bar, not behind it
        this._emit(o);
      }
      _emit(o) { const R = o.mode === 'front' ? (this._frontRuns || { n: 0 }) : null; this.dispatchEvent(new CustomEvent('vera:xpl:rendered', { detail: { mode: o.mode, stations: o.stations, cards: o.cards.length, panels: o.panels.length, graphs: (o.graphs || []).length, gnodes: (o.gnodes || []).length, anodes: (o.anodes || []).length, enodes: (o.enodes || []).length, layers: Object.assign({}, this._S.layers, { galaxy: !!this._S.galaxy }), edges: (o.edges || []).length, runs: R ? R.n : (o.runs || 0), outline: (o.outline || []).length, focus: o.focus == null ? null : o.focus, pending: (o.pending || []).length, solo: !!o.solo, stack: !!o.stack, tilt: o.tilt, azim: o.azim, inv: o.inv || 1 }, bubbles: true })); }
    }
    root.customElements.define('vera-exploded', VeraExploded);
  }
  const api = { layout, frontRuns, LAYERS, ensureCss, ensureIso, graphData, cardsRouter, isoRouter, landRuns, actsOf, estOf, actParent, actTree, actRuns, planeSize, widgetOf, groupOf, valueOf, isoBody, faceHtml, diagramHtml, ICON, version: 12 };
  root.VeraExploded = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
