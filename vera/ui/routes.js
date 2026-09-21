/* vera/ui/routes.js — the ONE set of run routers the exploded views draw with (UI redesign, the Canvas board's rule:
   "edges live BEHIND the cards and route down a gutter, never diagonally"; in iso "every leg changes exactly one of
   u, v, z"). Served at /ui/routes.js (vera/ui/libs.py) as window.VeraRoutes; required beside the element in node.

   The chat's exploded scene (vera/chat/exploded_element.js) and the structured graph (<vera-structgraph>,
   vera/ui/structgraph_element.js) both route through this file, so a routing fix lands in both and neither holds a
   copy of the maths. Three routers and two layout helpers:

     cardsRouter(G)    the exploded scene's CARDS runs: collected, then laned per gutter — trunks, fanned ports, bus lanes
     isoRouter(G)      the lattice's own lanes: three legs, each changing exactly one ground coordinate
     channelRouter(G)  the structured graph's runs: vertical only in the GUTTERS between columns, horizontal only in the
                       CHANNELS between bands — "along the lanes of their own plane, then plumb" (the Live Operations
                       board) — every leg booked into a corridor slot so no two legs share a length, the slot order
                       chosen by a local crossing count, the ports on a card fanned in lane order
     rank(ids, edges)  longest-path layering of a directed graph (cycles broken on DFS back edges, which are reported)
     order(cells, …)   barycentre ordering of the items of each cell by where their neighbours sit

   Every router is pure: it is handed the geometry (where the columns, gutters, channels and boxes are) and an edge()
   sink, and emits rotated segments {x, y, len, deg} — the same shape the exploded scene draws. Node-testable:
   tests/test_exploded_routing.cjs (cards, iso) and tests/test_structgraph_layout.cjs (channel). */
(function (root) {
  'use strict';
  const px = (v) => Math.round(v * 10) / 10;
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

  /* ── RANK: longest-path layering ──────────────────────────────────────────────────────────────────────────────
     ids in, {rank: Map id → column, back: [edge], depth} out. A source (nothing points at it) is column 0; every other
     item sits one past the furthest item that points at it, so a dependency always reads left → right. A cycle would
     stall that, so the edges a depth-first walk finds pointing back into the walk are left out of the ranking and
     handed back as `back` — the caller draws them as what they are (a call back up the chain), not as a rank. */
  function rank(ids, edges) {
    const idx = new Map(); ids.forEach((id, i) => idx.set(String(id), i)); const n = ids.length;
    const adj = ids.map(() => []), fwd = ids.map(() => []), back = [];
    (edges || []).forEach((e) => { const a = idx.get(String(e.from)), b = idx.get(String(e.to)); if (a == null || b == null || a === b) return; adj[a].push({ b, e }); });
    const st = new Array(n).fill(0);
    const dfs = (u0) => { const stack = [[u0, 0]]; st[u0] = 1;
      while (stack.length) { const top = stack[stack.length - 1], u = top[0]; if (top[1] >= adj[u].length) { st[u] = 2; stack.pop(); continue; }
        const q = adj[u][top[1]++]; if (st[q.b] === 1) { back.push(q.e); continue; } fwd[u].push(q.b); if (!st[q.b]) { st[q.b] = 1; stack.push([q.b, 0]); } } };
    for (let i = 0; i < n; i++) if (!st[i]) dfs(i);
    const indeg = new Array(n).fill(0); fwd.forEach((L) => L.forEach((v) => { indeg[v]++; }));
    const r = new Array(n).fill(0), q = []; for (let i = 0; i < n; i++) if (!indeg[i]) q.push(i);
    while (q.length) { const u = q.shift(); fwd[u].forEach((v) => { r[v] = Math.max(r[v], r[u] + 1); if (!--indeg[v]) q.push(v); }); }
    const out = new Map(); ids.forEach((id, i) => out.set(String(id), r[i]));
    return { rank: out, back, depth: n ? Math.max.apply(null, r) + 1 : 0 };
  }

  /* ── ORDER: barycentre ordering of each cell ──────────────────────────────────────────────────────────────────
     cells: arrays of ids (the items that share one cell, in their current order); yOf(id): where any item sits now.
     Each cell comes back sorted by the mean position of its members' neighbours OUTSIDE the cell — an item with no
     neighbour keeps its own position as its key, so it stays where it was among the others. Two passes of this over
     a placed layout is what lines a caller up with its callees and keeps the runs between them short. */
  function order(cells, edges, yOf) {
    const nb = {}; (edges || []).forEach((e) => { const a = String(e.from), b = String(e.to); if (a === b) return; (nb[a] = nb[a] || []).push(b); (nb[b] = nb[b] || []).push(a); });
    return cells.map((cell) => { const ids = cell.map(String), inCell = new Set(ids), key = {};
      ids.forEach((id) => { const ys = (nb[id] || []).filter((o) => !inCell.has(o)).map(yOf).filter((y) => isFinite(y)); key[id] = ys.length ? ys.reduce((s, y) => s + y, 0) / ys.length : yOf(id); });
      return ids.slice().sort((a, b) => (key[a] - key[b]) || (ids.indexOf(a) - ids.indexOf(b))); });
  }

  /* ── the CHANNEL router: gutters and channels ─────────────────────────────────────────────────────────────────
     G: { gutters: [{x0, x1}]   gutter g lies LEFT of column g; the last one is the right margin
          channels: [{y0, y1}]  channel c lies ABOVE band c; the last one is the floor under the last band
          pitch                 lane pitch inside a corridor (px)
          edge(a, b, col, cls, title), out: { edges, runs, ports } }
     A box: { id, cx, cy, w, h, col, band } — col its column, band the index of its TOP-LEVEL band.
     A run leaves its source at the RIGHT side and enters its target at the LEFT side, always. Between adjacent columns
     that is one vertical leg in the gutter between them. Otherwise the run drops (or climbs) in the gutter right of the
     source to a CHANNEL — the one on the target band's edge that faces the source (the nearer edge of their own band
     when they share one) — travels the channel to the gutter left of the target, and plumbs to the target's row. A
     run bound back up the chain (target column ≤ source column) is the same path with the channel travelled leftward.
     Corridor legs are booked into SLOTS: legs whose extents overlap never share a slot, so no two legs share a length;
     the slot order is chosen leg by leg by the crossings it would cost against the legs already there (a leg's stubs
     against the others' lengths), longest legs placed first. Ports fan a card's side in lane order — a leg whose
     length runs UP from the port takes an upper port, inner lane first; one running DOWN a lower port, outer lane
     first — which is the order in which no stub crosses a neighbour's length. Channels are laned first (their
     stubs stand on the gutters' centres), then the gutters with the channels' exact rows.
     Pure. demand() answers how many slots every corridor needed, so a caller can widen and lay out again. */
  function channelRouter(G) {
    const runs = []; const LP = G.pitch || 9;
    const add = (A, B, col, cls, title, o) => { if (A && B) runs.push(Object.assign({ A, B, col, cls: cls || '', title: title || '' }, o || {})); };
    const need = { gutters: G.gutters.map(() => 0), channels: G.channels.map(() => 0) };
    const mid = (c) => (c.x0 != null ? (c.x0 + c.x1) / 2 : (c.y0 + c.y1) / 2);
    // crossings a leg P's length makes with the stubs of Q, and Q's with P's, given their lanes qp and qq
    const cross = (P, qp, Q, qq) => { let n = 0;
      const hit = (L, ql, S, qs) => { const lo = Math.min(L.p0, L.p1) - 0.01, hi = Math.max(L.p0, L.p1) + 0.01;
        [[S.inP, S.inSide], [S.outP, S.outSide]].forEach((st) => { if (st[0] <= lo || st[0] >= hi) return; if (st[1] === 'lo' ? ql < qs : ql > qs) n++; }); };
      hit(P, qp, Q, qq); hit(Q, qq, P, qp); return n; };
    // lane the legs of one corridor: an order by insertion (fewest crossings, ties outer), then slots by overlap
    const lane = (legs, c0, c1) => { if (!legs.length) return 0;
      const L = legs.slice().sort((a, b) => (Math.abs(b.p1 - b.p0) - Math.abs(a.p1 - a.p0)) || (Math.min(a.p0, a.p1) - Math.min(b.p0, b.p1)));
      const ord = [];
      L.forEach((leg) => { let best = 0, bestN = Infinity;
        for (let pos = 0; pos <= ord.length; pos++) { let n = 0; ord.forEach((o, i) => { n += cross(leg, pos - 0.5, o, i); }); if (n < bestN || (n === bestN && pos === ord.length)) { bestN = n; best = pos; } }
        ord.splice(best, 0, leg); });
      // slots: a leg takes the lowest slot no overlapping leg holds (overlap padded by the card halves it ends on)
      const slots = [];
      ord.forEach((leg) => { const lo = Math.min(leg.p0, leg.p1) - (leg.pad || 0), hi = Math.max(leg.p0, leg.p1) + (leg.pad || 0); let s = 0;
        for (; s < slots.length; s++) if (!slots[s].some((o) => Math.min(o.p0, o.p1) - (o.pad || 0) < hi && Math.max(o.p0, o.p1) + (o.pad || 0) > lo)) break;
        (slots[s] = slots[s] || []).push(leg); leg.slot = s; });
      const S = slots.length, centre = (c0 + c1) / 2;
      ord.forEach((leg) => { leg.q = centre + (leg.slot - (S - 1) / 2) * LP; });
      return S; };
    const flush = () => {
      const GU = G.gutters, CH = G.channels; const legsG = GU.map(() => []), legsC = CH.map(() => []);
      // 1. every run as corridor legs, provisional coordinates at the corridors' centres
      runs.forEach((r) => { const A = r.A, B = r.B, i = A.col, j = B.col; r.rid = G.out.runs++; r.ax = A.cx + A.w / 2 + 3; r.bx = B.cx - B.w / 2 - 3; r.ya = A.cy; r.yb = B.cy;
        if (j === i + 1) { r.gA = i + 1; r.vA = { r, p0: A.cy, p1: B.cy, inP: A.cy, inSide: 'lo', outP: B.cy, outSide: 'hi', pad: Math.max(A.h, B.h) / 2 }; legsG[r.gA].push(r.vA); return; }
        const a = A.band, b = B.band; let c;
        if (a < b) c = b; else if (a > b) c = b + 1; else { const top = CH[a].y1, bot = CH[a + 1].y0; c = (A.cy + B.cy) / 2 < (top + bot) / 2 ? a : a + 1; }
        r.gA = i + 1; r.gB = j; r.c = c; const cy = mid(CH[c]), xa = mid(GU[r.gA]), xb = mid(GU[r.gB]);
        r.vA = { r, p0: A.cy, p1: cy, inP: A.cy, inSide: 'lo', outP: cy, outSide: r.gB > r.gA ? 'hi' : 'lo', pad: A.h / 2 };
        r.h = { r, p0: xa, p1: xb, inP: xa, inSide: cy > A.cy ? 'lo' : 'hi', outP: xb, outSide: cy > B.cy ? 'lo' : 'hi', pad: 0 };
        r.vB = { r, p0: cy, p1: B.cy, inP: cy, inSide: r.gB > r.gA ? 'lo' : 'hi', outP: B.cy, outSide: 'hi', pad: B.h / 2 };
        legsG[r.gA].push(r.vA); legsC[c].push(r.h); legsG[r.gB].push(r.vB); });
      // 2. the channels' lanes, then the gutters' with the channels' exact rows
      legsC.forEach((L, c) => { need.channels[c] = lane(L, CH[c].y0, CH[c].y1); });
      runs.forEach((r) => { if (!r.h) return; const cy = r.h.q; r.vA.p1 = cy; r.vA.outP = cy; r.vB.p0 = cy; r.vB.inP = cy; });
      legsG.forEach((L, g) => { need.gutters[g] = lane(L, GU[g].x0, GU[g].x1); });
      runs.forEach((r) => { if (!r.h) return; r.h.p0 = r.vA.q; r.h.inP = r.vA.q; r.h.p1 = r.vB.q; r.h.outP = r.vB.q; });
      // 3. ports: a card's side fanned in lane order — up-legs first (inner lane first), then down-legs (outer first)
      const sides = {};
      runs.forEach((r) => { const kA = r.A.id + '|R', kB = r.B.id + '|L';
        const endA = r.vA.p1;                                   // where the length leaving A's port ends: the channel's row, or B's row
        (sides[kA] = sides[kA] || { box: r.A, side: 'R', L: [] }).L.push({ r, end: 'a', d: Math.abs(r.vA.q - r.ax), up: endA < r.A.cy - 0.5 });
        const vb = r.vB || r.vA, from = vb.p0;                  // where the length arriving at B's port comes from
        (sides[kB] = sides[kB] || { box: r.B, side: 'L', L: [] }).L.push({ r, end: 'b', d: Math.abs(vb.q - r.bx), up: from < r.B.cy - 0.5 }); });
      Object.keys(sides).forEach((k) => { const S = sides[k], n = S.L.length, box = S.box;
        const up = S.L.filter((e) => e.up).sort((a, b) => a.d - b.d), rest = S.L.filter((e) => !e.up).sort((a, b) => b.d - a.d);
        const P = up.concat(rest), pf = n > 1 ? Math.min(10, Math.max(4, (box.h - 10) / (n - 1))) : 0;
        P.forEach((e, i) => { const y = box.cy + (i - (n - 1) / 2) * pf; if (e.end === 'a') { e.r.ya = y; e.r.vA.p0 = y; } else { e.r.yb = y; (e.r.vB || e.r.vA).p1 = y; }
          G.out.ports.push({ id: box.id, side: S.side, x: px(S.side === 'R' ? box.cx + box.w / 2 : box.cx - box.w / 2), y: px(y), run: e.r.rid }); }); });
      // 4. the legs
      runs.forEach((r) => { let W;
        if (!r.h) W = [[r.ax, r.ya], [r.vA.q, r.ya], [r.vA.q, r.yb], [r.bx, r.yb]];
        else W = [[r.ax, r.ya], [r.vA.q, r.ya], [r.vA.q, r.h.q], [r.vB.q, r.h.q], [r.vB.q, r.yb], [r.bx, r.yb]];
        for (let n = 0; n + 1 < W.length; n++) { const a = { x: W[n][0], y: W[n][1] }, b = { x: W[n + 1][0], y: W[n + 1][1] }; if (Math.abs(a.x - b.x) + Math.abs(a.y - b.y) < 0.5) continue;
          G.edge(a, b, r.col, r.cls, r.title); const seg = G.out.edges[G.out.edges.length - 1]; seg.run = r.rid; if (r.from != null) { seg.from = r.from; seg.to = r.to; } } });
      runs.length = 0;
    };
    return { add, flush, demand: () => need, pending: () => runs.length };
  }

  const api = { cardsRouter, isoRouter, channelRouter, rank, order, version: 1 };
  root.VeraRoutes = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
