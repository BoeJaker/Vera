/* VeraDesk - the desk's layout rules, pure: no DOM, no state, the same in the browser (window.VeraDesk) and in Node.

   The owner approved the desk on 2026-10-06 (shared planning context-and-ui, DECISIONS; design board 9): everything sits
   on one desk in layers - graph, a UI panel, the items, the chat - and the chat is the centre of focus.

     chatRect(dock, at, desk)             where the chat sits: docked left / right / bottom, or floating (at = [l, t])
     snapEdge(rect, desk)                 the edge a dragged chat docks to when let go there, or null
     sizeAt(R, x, y, o, desk)             an item's face and size at (x, y): its distance to the chat as a share of the
                                          space free on that side; o.size (the person's size) holds until ~93% out,
                                          o.pinned holds everywhere
     arrange(mode, R, items, desk)        {id: [x, y]} - 'orbit' | 'kind' | 'timeline' | 'stack'; items come ranked,
                                          most relevant first, each {id, kind, age}; docked chats get bands
     pushOut(R, x, y, sizeFn, desk)       the item moved out of the chat's path: {x, y, size, pushed}
     stackOrder(nd, o)                    z-index: closer to the chat = higher; dragged and fullscreen on top
     fullRegion(R, desk)                  the largest part of the desk the chat does not cover
     layerOrder(o)                        back to front: graph, panel, items, chat (o.panelFront: the panel over the items)
     makeRoom(grid, obstacles, widgets, o) panel widgets: each in the free cell nearest its home, a compact 1x1 face
                                          before it is ever covered

   A desk is {W, H, m (margin), top (below the top controls), edgeX, edgeTop, edgeBottom}; defaults are board 9's. */
(function (root) {
  'use strict';

  var DESK = { W: 1600, H: 900, m: 16, top: 70, edgeX: 80, edgeTop: 96, edgeBottom: 34 };
  var CHAT_FLOAT = { w: 440, h: 470 }, CHAT_SIDE_W = 440, CHAT_BOTTOM = { w: 760, h: 274 };
  var SIZE = { chip: { w: 150, h: 34 }, max: { w: 300, h: 196 } };
  var NEAR = 0.28, FAR = 0.85, USER_FAR = 0.93;

  function desk(d) { var o = {}, k; for (k in DESK) o[k] = DESK[k]; if (d) for (k in d) o[k] = d[k]; return o; }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function chatRect(dock, at, d) {
    d = desk(d);
    if (dock === 'left') return { l: d.m, t: d.top, r: d.m + CHAT_SIDE_W, b: d.H - d.m };
    if (dock === 'right') return { l: d.W - d.m - CHAT_SIDE_W, t: d.top, r: d.W - d.m, b: d.H - d.m };
    if (dock === 'bottom') { var l = Math.round((d.W - CHAT_BOTTOM.w) / 2); return { l: l, t: d.H - d.m - CHAT_BOTTOM.h, r: l + CHAT_BOTTOM.w, b: d.H - d.m }; }
    if (at) return { l: at[0], t: at[1], r: at[0] + CHAT_FLOAT.w, b: at[1] + CHAT_FLOAT.h };
    var cl = Math.round((d.W - CHAT_FLOAT.w) / 2), ct = Math.round((d.H - CHAT_FLOAT.h) / 2);
    return { l: cl, t: ct, r: cl + CHAT_FLOAT.w, b: ct + CHAT_FLOAT.h };
  }

  function snapEdge(rect, d) {
    d = desk(d);
    if (rect.l < 40) return 'left';
    if (rect.r > d.W - 40) return 'right';
    if (rect.b > d.H - 20) return 'bottom';
    return null;
  }

  function faceOf(w, h) { return h >= 150 && w >= 250 ? 'max' : h >= 64 ? 'card' : 'chip'; }

  /* how far (x, y) is from the chat as a share of the space free on that side, across and up/down apart */
  function distance(R, x, y, d) {
    d = desk(d);
    var dx = x < R.l ? R.l - x : x > R.r ? x - R.r : 0, dy = y < R.t ? R.t - y : y > R.b ? y - R.b : 0;
    var spanX = Math.max(1, x < R.l ? R.l - d.edgeX : (d.W - d.edgeX) - R.r);
    var spanY = Math.max(1, y < R.t ? R.t - d.edgeTop : (d.H - d.edgeBottom) - R.b);
    return Math.sqrt(Math.pow(dx / spanX, 2) + Math.pow(dy / spanY, 2));
  }

  function sizeAt(R, x, y, o, d) {
    o = o || {};
    var nd = distance(R, x, y, d);
    var t = clamp((FAR - nd) / (FAR - NEAR), 0, 1);
    t = 1 - Math.pow(1 - t, 2);                         /* large sizes hold over most of the way */
    var face = t > 0.6 ? 'max' : t > 0.25 ? 'card' : 'chip';
    /* d.scale grows or shrinks the larger faces with the desk (a narrow canvas column, a wide screen); a chip keeps its
       size so its title stays readable */
    var sc = (d && d.scale) || 1, mw = SIZE.max.w * sc, mh = SIZE.max.h * sc;
    var w = Math.round(SIZE.chip.w + t * (mw - SIZE.chip.w)), h = Math.round(SIZE.chip.h + t * (mh - SIZE.chip.h));
    var why = '';
    if (o.size && o.pinned) { w = o.size.w; h = o.size.h; face = faceOf(w, h); t = 1; why = 'pinned'; }
    else if (o.size && nd < USER_FAR) { w = o.size.w; h = o.size.h; face = faceOf(w, h); t = 1; why = 'your size'; }
    return { face: face, w: w, h: h, t: t, nd: nd, why: why };
  }

  function edgeSlots(R, d) {
    // spaced by a chip's own size, so chips at the border never sit on each other
    var s = [], i, cw = SIZE.chip.w + 10, ch = SIZE.chip.h + 6;
    var nRow = Math.max(1, Math.floor((d.W * 0.8) / cw)), nCol = Math.max(1, Math.floor((d.H - d.edgeTop - 60) / ch));
    for (i = 0; i < nRow; i++) s.push([d.W * 0.1 + cw / 2 + i * cw, d.H - d.edgeBottom]);
    for (i = 0; i < nCol; i++) { var y = d.edgeTop + 10 + i * ch; s.push([d.edgeX + SIZE.chip.w / 2 - 10, y]); s.push([d.W - SIZE.chip.w / 2 - 8, y]); }
    var clear = s.filter(function (p) {
      var dx = Math.max(R.l - p[0], 0, p[0] - R.r), dy = Math.max(R.t - p[1], 0, p[1] - R.b);
      return Math.sqrt(dx * dx + dy * dy) > 140;
    });
    /* a border slot is one where an item really is a chip - far from the chat - or a docked chat's bottom row puts
       full-size items under its elbow (the canvas column, 2026-10-06: 12 of 20 came out full size) */
    var far = clear.filter(function (p) { return distance(R, p[0], p[1], d) >= 0.78; });
    return far.length >= 4 ? far : clear;
  }

  function arrange(mode, R, items, d) {
    d = desk(d);
    var W = d.W, H = d.H, cx = (R.l + R.r) / 2, cy = (R.t + R.b) / 2, hw = (R.r - R.l) / 2, hh = (R.b - R.t) / 2;
    var pos = {}, chips = [];
    var dockedLeft = R.l < 200, dockedRight = R.r > W - 200, below = R.t > H * 0.55;
    var sides = dockedLeft ? [1] : dockedRight ? [-1] : [1, -1];
    function cl(x, y) { return [clamp(x, Math.min(90, W / 2), Math.max(W / 2, W - 90)), clamp(y, Math.min(60, H / 2), Math.max(H / 2, H - 40))]; }
    if (mode === 'orbit' && (dockedLeft || dockedRight || below)) {
      /* BANDS, ONE AFTER ANOTHER away from the chat, each as deep as the items it holds are at that distance (sizeAt
         there), each holding as many as its length fits at that size, and none reaching into the border's chip strip.
         Fractions of the free side overlapped in a narrow desk (the canvas column, 2026-10-06: 21 overlapping pairs). */
      var dir = dockedLeft ? 1 : dockedRight ? -1 : 0, k = 0, gapB = 14;
      var vertical = dir === 0;                                  /* bottom dock: bands are rows above the chat */
      var reserve = vertical ? SIZE.chip.h + 30 : SIZE.chip.w + 20;   /* the border strip at the far side */
      var cursor = vertical ? R.t - gapB : (dir > 0 ? R.r + gapB : R.l - gapB);
      var lengthAlong = vertical ? W - 2 * d.m : H - 2 * d.m;
      for (var band = 0; band < 4 && k < items.length; band++) {
        var probe = vertical ? sizeAt(R, W / 2, cursor - 60, {}, d) : sizeAt(R, cursor + dir * 80, H / 2, {}, d);
        var depth = vertical ? probe.h : probe.w;
        var centre = vertical ? cursor - depth / 2 : cursor + dir * depth / 2;
        var real = vertical ? sizeAt(R, W / 2, centre, {}, d) : sizeAt(R, centre, H / 2, {}, d);
        if (real.face === 'chip') break;                          /* past the cards: the rest are border chips */
        depth = vertical ? real.h : real.w;
        centre = vertical ? cursor - depth / 2 : cursor + dir * depth / 2;
        var farEdge = vertical ? centre - depth / 2 : centre + dir * depth / 2;
        if (vertical ? farEdge < d.edgeTop + reserve : (dir > 0 ? farEdge > W - reserve : farEdge < reserve)) break;
        var cross = vertical ? real.w : real.h;
        var count = Math.max(1, Math.min(band === 0 ? 3 : 4, Math.floor(lengthAlong / (cross + gapB))));
        for (var q = 0; q < count && k < items.length; q++) {
          var it = items[k++], at = (q + 0.5) / count;
          pos[it.id] = vertical ? cl(d.m + at * lengthAlong, centre) : cl(centre, d.m + at * lengthAlong);
        }
        cursor = vertical ? farEdge - gapB : farEdge + dir * gapB;
      }
      chips = items.slice(k);
    } else if (mode === 'orbit') {
      var ring = function (off, n, start) {
        var out = [];
        for (var i = 0; i < n; i++) {
          var a = start + i * 2 * Math.PI / n, x = cx + (hw + off) * Math.cos(a), y = cy + (hh + off) * Math.sin(a);
          if (x > 170 && x < W - 170 && y > 120 && y < H - 110) out.push([x, y]);
        }
        return out;
      };
      var near = ring(180, 8, 0.05), mid = ring(400, 14, 0.3), n = 0, m2 = 0;
      items.forEach(function (it, i) {
        if (i < 2 && n < near.length) pos[it.id] = near[n++];
        else if (i < 6 && m2 < mid.length) pos[it.id] = mid[m2++];
        else chips.push(it);
      });
    } else if (mode === 'kind') {
      var kinds = [];
      items.forEach(function (it) { if (kinds.indexOf(it.kind) < 0) kinds.push(it.kind); });
      kinds.forEach(function (kd, ki) {
        var side = sides[ki % sides.length], col = Math.floor(ki / sides.length);
        items.filter(function (it) { return it.kind === kd; }).forEach(function (it, mi) {
          if (below) { var x = 200 + ki * 200, y = R.t - 180 - mi * 120; if (y < 80 || x > W - 100) chips.push(it); else pos[it.id] = cl(x, y); return; }
          var x2 = side > 0 ? R.r + 170 + col * 230 : R.l - 170 - col * 230, y2 = 150 + mi * 130 + (ki % 2) * 40;
          if (x2 < 120 || x2 > W - 120 || col > 2) chips.push(it); else pos[it.id] = cl(x2, y2);
        });
      });
    } else if (mode === 'timeline') {
      var per = {};
      items.forEach(function (it) {
        var age = it.age == null ? 9 : it.age;
        if (age < 0 || age > 3) { chips.push(it); return; }
        var kk = per[age] = (per[age] || 0) + 1;
        if (below) pos[it.id] = cl(220 + (kk - 1) * 260, R.t - 170 - age * 140);
        else pos[it.id] = cl(sides[0] > 0 ? R.r + 180 + age * 210 : R.l - 180 - age * 210, 170 + (kk - 1) * 150);
      });
    } else {                                               /* stack */
      items.forEach(function (it, i) {
        if (i < 3) pos[it.id] = below ? cl(R.l + 140 + i * 250, R.t - 120) : cl(sides[0] > 0 ? R.r + 175 : R.l - 175, 170 + i * 205);
        else if (i < 7) pos[it.id] = below ? cl(R.l + 40 + (i - 3) * 230, R.t - 400) : cl(sides[0] > 0 ? R.r + 420 : R.l - 420, 140 + (i - 3) * 150);
        else chips.push(it);
      });
    }
    var slots = edgeSlots(R, d);
    chips.forEach(function (it, i) { if (slots.length) pos[it.id] = slots[i % slots.length]; });
    return pos;
  }

  function pushOut(R, x, y, sizeFn, d) {
    d = desk(d);
    var s = sizeFn(x, y), pushed = false, gap = 14;
    for (var k = 0; k < 3; k++) {
      var il = x - s.w / 2, ir = x + s.w / 2, it = y - s.h / 2, ib = y + s.h / 2;
      if (ir <= R.l - gap || il >= R.r + gap || ib <= R.t - gap || it >= R.b + gap) break;
      var pl = ir - (R.l - gap), pr = (R.r + gap) - il, pu = ib - (R.t - gap), pd = (R.b + gap) - it;
      var moves = [[-pl, 0, x - pl - s.w / 2 >= d.m], [pr, 0, x + pr + s.w / 2 <= d.W - d.m],
                   [0, -pu, y - pu - s.h / 2 >= d.top + 20], [0, pd, y + pd + s.h / 2 <= d.H - 10]]
        .filter(function (v) { return v[2]; })
        .sort(function (a, b) { return Math.abs(a[0] + a[1]) - Math.abs(b[0] + b[1]); });
      if (!moves.length) break;
      x += moves[0][0]; y += moves[0][1]; pushed = true;
      s = sizeFn(x, y);
    }
    return { x: x, y: y, size: s, pushed: pushed };
  }

  function stackOrder(nd, o) {
    o = o || {};
    if (o.dragging) return 200;
    if (o.full) return 150;
    return 10 + Math.round((1 - Math.min(nd || 0, 1.2) / 1.2) * 100);
  }

  function fullRegion(R, d) {
    d = desk(d);
    var regs = [{ l: d.m, t: d.top, r: R.l - d.m, b: d.H - d.m }, { l: R.r + d.m, t: d.top, r: d.W - d.m, b: d.H - d.m },
                { l: d.m, t: d.top, r: d.W - d.m, b: R.t - d.m }, { l: d.m, t: R.b + d.m, r: d.W - d.m, b: d.H - d.m }]
      .filter(function (g) { return g.r - g.l > 200 && g.b - g.t > 150; })
      .sort(function (a, b) { return (b.r - b.l) * (b.b - b.t) - (a.r - a.l) * (a.b - a.t); });
    return regs[0] || { l: d.m, t: d.top, r: d.W - d.m, b: d.H - d.m };
  }

  /* grid = {ox, oy, cols, rows, cw, ch, gap}; obstacles = [{l,t,r,b}] in the same space; widgets = [{id, w, h}] in
     cells, in priority order. o.makeRoom false keeps every widget at home. Returns [{id, c, r, w, h, compact, moved,
     covered, rect}]. */
  function makeRoom(grid, obstacles, widgets, o) {
    o = o || {};
    var mr = o.makeRoom !== false, taken = {}, homeTaken = {}, home = {};
    function rectOf(c, r, w, h) {
      var l = grid.ox + c * (grid.cw + grid.gap), t = grid.oy + r * (grid.ch + grid.gap);
      return { l: l, t: t, r: l + w * grid.cw + (w - 1) * grid.gap, b: t + h * grid.ch + (h - 1) * grid.gap };
    }
    function hits(a) { return (obstacles || []).some(function (b) { return a.l < b.r && a.r > b.l && a.t < b.b && a.b > b.t; }); }
    function free(map, c, r, w, h) {
      if (c + w > grid.cols || r + h > grid.rows) return false;
      for (var i = 0; i < w; i++) for (var j = 0; j < h; j++) if (map[(c + i) + ',' + (r + j)]) return false;
      return true;
    }
    function take(map, c, r, w, h) { for (var i = 0; i < w; i++) for (var j = 0; j < h; j++) map[(c + i) + ',' + (r + j)] = 1; }
    widgets.forEach(function (wg) {
      for (var r = 0; r < grid.rows && !home[wg.id]; r++) for (var c = 0; c < grid.cols && !home[wg.id]; c++)
        if (free(homeTaken, c, r, wg.w, wg.h)) { home[wg.id] = [c, r]; take(homeTaken, c, r, wg.w, wg.h); }
    });
    function nearest(w, h, hm) {
      var best = null, bd = 1e9;
      for (var r = 0; r < grid.rows; r++) for (var c = 0; c < grid.cols; c++) {
        if (!free(taken, c, r, w, h) || hits(rectOf(c, r, w, h))) continue;
        var dd = Math.abs(c - hm[0]) + Math.abs(r - hm[1]);
        if (dd < bd) { bd = dd; best = [c, r]; }
      }
      return best;
    }
    var out = [];
    widgets.forEach(function (wg) {
      var hm = home[wg.id] || [0, 0], w = wg.w, h = wg.h, spot = null, compact = false;
      if (mr) {
        spot = nearest(w, h, hm);
        if (!spot && (w > 1 || h > 1)) { spot = nearest(1, 1, hm); if (spot) { compact = true; w = 1; h = 1; } }
      }
      if (!spot && free(taken, hm[0], hm[1], w, h)) spot = hm;
      for (var r = 0; r < grid.rows && !spot; r++) for (var c = 0; c < grid.cols && !spot; c++) if (free(taken, c, r, w, h)) spot = [c, r];
      if (!spot) return;
      take(taken, spot[0], spot[1], w, h);
      var rect = rectOf(spot[0], spot[1], w, h);
      out.push({ id: wg.id, c: spot[0], r: spot[1], w: w, h: h, compact: compact,
                 moved: spot[0] !== hm[0] || spot[1] !== hm[1], covered: hits(rect), rect: rect });
    });
    return out;
  }

  /* the layers, back to front: the graph, the UI panel, the items, the chat - or, with the panel brought to the front
     (owner, 2026-10-06: "like the fullscreen for desk elements"), the panel over the items and still under the chat */
  function layerOrder(o) {
    o = o || {};
    var L = ['graph'], panel = o.panel !== false;
    if (panel && !o.panelFront) L.push('panel');
    L.push('items');
    if (panel && o.panelFront) L.push('panel');
    L.push('chat');
    return L;
  }

  var api = { DESK: DESK, layerOrder: layerOrder, chatRect: chatRect, snapEdge: snapEdge, distance: distance, sizeAt: sizeAt, faceOf: faceOf,
              arrange: arrange, pushOut: pushOut, stackOrder: stackOrder, fullRegion: fullRegion, makeRoom: makeRoom };
  root.VeraDesk = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
