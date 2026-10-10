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
  var SIZE = { icon: { w: 34, h: 34 }, chip: { w: 150, h: 34 }, max: { w: 300, h: 196 } };
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
    /* d.scaleY, when given, grows the faces' height apart from their width: a tall desk beside a docked chat fills its
       bands' length with taller cards rather than leaving half of it empty */
    var sc = (d && d.scale) || 1, mw = SIZE.max.w * sc, mh = SIZE.max.h * ((d && d.scaleY) || sc);
    var w = Math.round(SIZE.chip.w + t * (mw - SIZE.chip.w)), h = Math.round(SIZE.chip.h + t * (mh - SIZE.chip.h));
    var why = '';
    if (o.size && o.pinned) { w = o.size.w; h = o.size.h; face = faceOf(w, h); t = 1; why = 'pinned'; }
    else if (o.size && nd < USER_FAR) { w = o.size.w; h = o.size.h; face = faceOf(w, h); t = 1; why = 'your size'; }
    else if (o.icon && face !== 'max') { w = SIZE.icon.w; h = SIZE.icon.h; face = 'icon'; why = 'the border is full'; }
    return { face: face, w: w, h: h, t: t, nd: nd, why: why };
  }

  function edgeSlots(R, d, need) {
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
    /* a slot on top of one already taken is no slot: on a narrow desk the columns stepping in from one side reach the
       other side's border column (the desk matrix, 2026-10-07: 465 against 477, chips printed over each other) */
    var onTaken = function (list, p) { return list.some(function (q) { return Math.abs(q[0] - p[0]) < cw && Math.abs(q[1] - p[1]) < ch; }); };
    /* not enough far slots for the chips (a floating chat leaves little "far"): further columns inward at both sides */
    for (var col = 1; far.length < (need || 0) && col < 6; col++) {
      for (i = 0; i < nCol; i++) {
        var yy = d.edgeTop + 10 + i * ch, inset = col * (SIZE.chip.w + 10);
        [[d.edgeX + SIZE.chip.w / 2 - 10 + inset, yy], [d.W - SIZE.chip.w / 2 - 8 - inset, yy]].forEach(function (q) {
          var dx = Math.max(R.l - q[0], 0, q[0] - R.r), dy = Math.max(R.t - q[1], 0, q[1] - R.b);
          var on = q[0] - SIZE.chip.w / 2 >= 0 && q[0] + SIZE.chip.w / 2 <= d.W;   /* a tiny desk: never off its far side */
          if (on && Math.sqrt(dx * dx + dy * dy) > 140 && distance(R, q[0], q[1], d) >= 0.78 && !onTaken(far, q)) far.push(q);
        });
      }
    }
    /* still short (a narrow desk: 18 chips, 1366 px): fill from a chip-sized grid over the rest of the desk, farthest
       first, only where an item really is a chip, never on a slot already taken - the chips wrapped round onto the
       same slots and printed over each other (the desk matrix, 2026-10-07: 15 overlapping pairs) */
    if (far.length < (need || 0)) {
      var taken = far.length >= Math.min(4, need || 4) ? far.slice() : clear.slice();
      var grid = [];
      for (var gy = d.edgeTop + 10; gy <= d.H - d.edgeBottom; gy += ch)
        for (var gx = d.edgeX + SIZE.chip.w / 2 - 10; gx <= d.W - SIZE.chip.w / 2 - 8; gx += cw) {
          var ex = Math.max(R.l - gx, 0, gx - R.r), ey = Math.max(R.t - gy, 0, gy - R.b);
          if (Math.sqrt(ex * ex + ey * ey) > 140 && sizeAt(R, gx, gy, {}, d).face === 'chip') grid.push([gx, gy]);
        }
      grid.sort(function (a, b) { return distance(R, b[0], b[1], d) - distance(R, a[0], a[1], d); });
      for (var gi = 0; gi < grid.length && taken.length < need; gi++) if (!onTaken(taken, grid[gi])) taken.push(grid[gi]);
      return taken;
    }
    return far.length >= Math.min(4, need || 4) ? far : clear;
  }

  /* groups -> bands, one after another away from the chat on each side it has (docked: one; below: rows above it;
     floating: both, the roomier first, alternating). A group fills one band; with spill its overflow takes the next
     band, otherwise it goes to the border. Returns {pos: {id: [x, y]}, chips: [items]}. */
  function bandRun(R, d, groups, spill) {
    var W = d.W, H = d.H, gapB = 14, pos = {}, chips = [];
    var dockedLeft = R.l < 200, dockedRight = R.r > W - 200, below = R.t > H * 0.55;
    var sides = dockedLeft ? [1] : dockedRight ? [-1] : below ? [0] : (W - R.r >= R.l ? [1, -1] : [-1, 1]);
    var cur = {}, open = {};
    sides.forEach(function (s) { cur[s] = s === 0 ? R.t - gapB : (s > 0 ? R.r + gapB : R.l - gapB); open[s] = true; });
    var along = function (s) { return s === 0 ? W - 2 * d.m : H - 2 * d.m; };
    function band(s, want) {
      var vertical = s === 0, dir = s, c = cur[s], rs = vertical ? SIZE.chip.h + 30 : SIZE.chip.w + 20;
      var probe = vertical ? sizeAt(R, W / 2, c - 60, {}, d) : sizeAt(R, c + dir * 80, H / 2, {}, d);
      var depth = vertical ? probe.h : probe.w, centre = vertical ? c - depth / 2 : c + dir * depth / 2;
      var real = vertical ? sizeAt(R, W / 2, centre, {}, d) : sizeAt(R, centre, H / 2, {}, d);
      if (real.face === 'chip') return null;                     /* past the cards: the rest are border chips */
      depth = vertical ? real.h : real.w; centre = vertical ? c - depth / 2 : c + dir * depth / 2;
      var far = vertical ? centre - depth / 2 : centre + dir * depth / 2;
      if (vertical ? far < d.edgeTop + rs : (dir > 0 ? far > W - rs : far < rs)) return null;
      var cross = vertical ? real.w : real.h;
      cur[s] = vertical ? far - gapB : far + dir * gapB;
      return { centre: centre, vertical: vertical, count: Math.max(1, Math.min(want, Math.floor(along(s) / (cross + gapB)))) };
    }
    var si = 0;
    groups.forEach(function (g) {
      var rest = g.slice();
      while (rest.length) {
        var s = null;
        for (var t = 0; t < sides.length; t++) { var cs = sides[(si + t) % sides.length]; if (open[cs]) { s = cs; si = (si + t + 1) % sides.length; break; } }
        if (s === null) { chips = chips.concat(rest); return; }
        var b = band(s, rest.length);
        if (!b) { open[s] = false; continue; }
        var L = along(s);
        for (var q = 0; q < b.count && rest.length; q++) { var it = rest.shift(), at = (q + 0.5) / b.count; pos[it.id] = b.vertical ? [d.m + at * L, b.centre] : [b.centre, d.m + at * L]; }
        if (!spill) { chips = chips.concat(rest); return; }
      }
    });
    return { pos: pos, chips: chips };
  }

  function arrange(mode, R, items, d) {
    d = desk(d);
    var W = d.W, H = d.H, cx = (R.l + R.r) / 2, cy = (R.t + R.b) / 2, hw = (R.r - R.l) / 2, hh = (R.b - R.t) / 2;
    var pos = {}, chips = [];
    /* OUT OF THE TOPIC, OUT TO THE BORDER (owner, 2026-10-10: the chat's primary topic decides "how close they are to chat
       or if they are moved all the way out of focus and minimised"): an item marked border takes no band beside the chat,
       however few the others are - it is a chip at the far border, after the items the arrangement places. */
    var outside = items.filter(function (it) { return it && it.border; });
    if (outside.length) items = items.filter(function (it) { return !(it && it.border); });
    var dockedLeft = R.l < 200, dockedRight = R.r > W - 200, below = R.t > H * 0.55;
    var sides = dockedLeft ? [1] : dockedRight ? [-1] : [1, -1];
    function cl(x, y) { return [clamp(x, Math.min(90, W / 2), Math.max(W / 2, W - 90)), clamp(y, Math.min(60, H / 2), Math.max(H / 2, H - 40))]; }
    /* EVERY ARRANGEMENT IS BANDS (2026-10-08). By kind, by time and stack used fixed pixel offsets from the 1600 px
       board and overlapped on a real desk (5-46 pairs); they now share orbit's bands: a band per group, as deep as its
       items are at that distance from the chat and holding as many as its length fits, none reaching the border's chip
       strip. A floating chat has bands on both sides, the roomier first; the rest are border chips. */
    if (mode === 'kind' || mode === 'timeline' || mode === 'stack' || (mode === 'orbit' && !(dockedLeft || dockedRight || below))) {
      var groups = [], spill = false;
      if (mode === 'kind') { var ks = []; items.forEach(function (it) { if (ks.indexOf(it.kind) < 0) ks.push(it.kind); }); groups = ks.map(function (kd) { return items.filter(function (it) { return it.kind === kd; }); }); }
      else if (mode === 'timeline') { for (var ag = 0; ag < 4; ag++) groups.push(items.filter(function (it) { return it.age === ag; })); items.forEach(function (it) { if (it.age == null || it.age < 0 || it.age > 3) chips.push(it); }); }
      else if (mode === 'stack') groups = [items.slice()];
      else { spill = true; for (var gi0 = 0, take = 3; gi0 < items.length; gi0 += take, take = 4) groups.push(items.slice(gi0, gi0 + take)); }
      var B = bandRun(R, d, groups, spill);
      Object.keys(B.pos).forEach(function (id) { pos[id] = cl(B.pos[id][0], B.pos[id][1]); });
      chips = chips.concat(B.chips);
    } else if (mode === 'orbit') {
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
    }
    chips = chips.concat(outside);
    var cells = iconCells(R, d, edgeSlots(R, d, chips.length), chips.length);
    chips.forEach(function (it, i) { if (cells.length) pos[it.id] = cells[i % cells.length]; });
    return pos;
  }

  /* MORE CHIPS THAN THE BORDER HOLDS (a narrow desk: 18 chips in a 423 x 407 column) - the farthest chip slots each
     become a row of four ICONS, the owner's "the border can be chips or icons for items" (2026-10-06); the chips
     wrapped round onto the same slots before and printed over each other (the desk matrix, 2026-10-07: 15 pairs).
     Returns [x, y] cells, an icon's as [x, y, 'icon']. */
  function iconCells(R, d, slots, need) {
    if (!slots.length || need <= slots.length) return slots;
    var conv = Math.min(slots.length, Math.ceil((need - slots.length) / 3));
    var byFar = slots.slice().sort(function (a, b) { return distance(R, b[0], b[1], d) - distance(R, a[0], a[1], d); });
    var gone = byFar.slice(0, conv), out = slots.filter(function (p) { return gone.indexOf(p) < 0; });
    gone.forEach(function (p) {
      for (var j = 0; j < 4; j++) {
        var x = clamp(p[0] + (j - 1.5) * (SIZE.icon.w + 4), SIZE.icon.w / 2, d.W - SIZE.icon.w / 2);
        out.push([x, p[1], 'icon']);
      }
    });
    return out;
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

  /* items = [{id, x, y, w, h, fixed?}] in priority order (the first keep their places). A later item overlapping an
     earlier one moves the shorter way out of it - away from the chat when that is no longer - and is re-sized where it
     lands (sizeFn(x, y, item) -> {w, h}); it never moves into the chat or off the desk. A few passes, as the
     floating chat's ring and its path pushed items onto each other (the canvas, 2026-10-07). Returns the items. */
  function separate(items, R, sizeFn, d) {
    d = desk(d);
    var gap = 8;
    /* two border faces (chips, icons) sit in rows 6 px apart and icons 4 px apart: an 8 px gap between them read the
       border's own spacing as an overlap and scattered it (the desk matrix, 2026-10-07) */
    function over(a, b) { var g = a.h <= 40 && b.h <= 40 ? 2 : gap; return a.x - a.w / 2 < b.x + b.w / 2 + g && b.x - b.w / 2 < a.x + a.w / 2 + g && a.y - a.h / 2 < b.y + b.h / 2 + g && b.y - b.h / 2 < a.y + a.h / 2 + g; }
    function inChat(it) { return it.x + it.w / 2 > R.l - gap && it.x - it.w / 2 < R.r + gap && it.y + it.h / 2 > R.t - gap && it.y - it.h / 2 < R.b + gap; }
    function fits(it) { return it.x - it.w / 2 >= 0 && it.x + it.w / 2 <= d.W && it.y - it.h / 2 >= 0 && it.y + it.h / 2 <= d.H; }
    function at(b, x, y) { var t = { x: x, y: y, w: b.w, h: b.h }, s = sizeFn ? sizeFn(x, y, b) : null; if (s) { t.w = s.w; t.h = s.h; } return t; }
    var placed = [];
    items.forEach(function (b) {
      var clear = function (t) { return fits(t) && !inChat(t) && !placed.some(function (a) { return over(a, t); }); };
      if (b.fixed || clear(b)) { placed.push(b); return; }
      /* the nearest clear spot, searched in widening rings round where it was put (re-sized at each, as its size
         depends on where it lands); none clear within reach: it stays, the least overlapped it can be */
      var best = null;
      for (var rad = 24; rad <= Math.max(d.W, d.H) && !best; rad += 24) {
        var n = Math.max(8, Math.round(rad / 12));
        for (var k = 0; k < n; k++) {
          var ang = (k / n) * 2 * Math.PI, t = at(b, b.x + rad * Math.cos(ang), b.y + rad * Math.sin(ang));
          if (clear(t)) { best = t; break; }
        }
      }
      /* NONE CLEAR AT THE SIZE ITS PLACE WOULD GIVE IT: searched again at the size it HAS. A chip pushed off the border by a
         card the person grew out to it was re-sized at every spot tried - near the chat it would be a full card, and no
         such room was left - so it stayed where it was, inside the grown card's right edge (owner, 2026-10-10: "another
         element can fall inside it from the right edge"). As it is, it fits somewhere clear; it keeps that size there. */
      if (!best && !b.fixed) {
        for (var rad2 = 24; rad2 <= Math.max(d.W, d.H) && !best; rad2 += 24) {
          var n2 = Math.max(8, Math.round(rad2 / 12));
          for (var k2 = 0; k2 < n2; k2++) {
            var ang2 = (k2 / n2) * 2 * Math.PI, t2 = { x: b.x + rad2 * Math.cos(ang2), y: b.y + rad2 * Math.sin(ang2), w: b.w, h: b.h };
            if (clear(t2)) { best = t2; best.keep = true; break; }
          }
        }
      }
      if (best) { b.x = best.x; b.y = best.y; b.w = best.w; b.h = best.h; if (best.keep) b.keepSize = true; }
      placed.push(b);
    });
    return items;
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
    /* a widget may say where it sits now (c, r): that is its home, so a widget nothing covers never moves - a dense flow
       worked out here did not quite match a real dashboard's, and every tile shifted (the panel make-room probe, 11/11) */
    widgets.forEach(function (wg) {
      if (wg.c != null && wg.r != null && free(homeTaken, wg.c, wg.r, wg.w, wg.h)) { home[wg.id] = [wg.c, wg.r]; take(homeTaken, wg.c, wg.r, wg.w, wg.h); }
    });
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
    var out = [], kept = {};
    /* first every widget nothing covers keeps its home: a covered one that moved first took the next one's home, and
       every tile below it shifted down in turn (a real dashboard: 11 of 11 moved for one covered tile) */
    if (mr) widgets.forEach(function (wg) { var hm = home[wg.id]; if (!hm) return;
      if (!hits(rectOf(hm[0], hm[1], wg.w, wg.h)) && free(taken, hm[0], hm[1], wg.w, wg.h)) { take(taken, hm[0], hm[1], wg.w, wg.h); kept[wg.id] = 1; } });
    widgets.forEach(function (wg) {
      var hm = home[wg.id] || [0, 0], w = wg.w, h = wg.h, spot = null, compact = false;
      if (kept[wg.id]) spot = hm;
      else {
        if (mr) {
          spot = nearest(w, h, hm);
          if (!spot && (w > 1 || h > 1)) { spot = nearest(1, 1, hm); if (spot) { compact = true; w = 1; h = 1; } }
        }
        if (!spot && free(taken, hm[0], hm[1], w, h)) spot = hm;
        for (var r = 0; r < grid.rows && !spot; r++) for (var c = 0; c < grid.cols && !spot; c++) if (free(taken, c, r, w, h)) spot = [c, r];
        if (!spot) return;
        take(taken, spot[0], spot[1], w, h);
      }
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

  /* THE DESK EXPLODED (owner-approved desk, 2026-10-06: "exploded mode integrated in the Live ops iso style"; "the graph
     at the back and visible in exploded mode"). The desk's layers become the iso lattice's planes, front first - the chat,
     the items, the UI panel, the context graph - and each thing on them a node that keeps its own id, so Explode and back
     is the same desk. Areas are the desk's own distances: beside the chat, at a glance, at the border.
       d = { items:[{key, label, face, layer, type, mid}], turns:[{mid, label}], graph:[{id, label}], focusMid }
       -> { planes:[{id, name}], areas:[...], nodes:[{id, label, plane, area, kind, detail, desk}], links:[{a, b, kind}] }
     A plane-area cell holds nine at most (the lattice stacks no more without overlap): a fuller cell spills into a
     neighbour area of the same name ("at the border · 2"), as Live ops splits its areas - every item is drawn. */
  var AREAS = ['beside the chat', 'at a glance', 'at the border'];
  function latticeOf(d) {
    d = d || {};
    var CELL = 9, nodes = [], links = [], cells = {}, cut = {}, areas = AREAS.slice();
    var areaOfFace = function (f) { return f === 'max' || f === 'layer-front' || f === 'layer-behind' ? 0 : f === 'card' ? 1 : 2; };
    var put = function (n) {
      var base = n.area, a = base, k2 = 1;
      while ((cells[n.plane + ':' + a] || 0) >= CELL && k2 < 8) { k2++; a = base + ' · ' + k2; }
      if ((cells[n.plane + ':' + a] || 0) >= CELL) { cut[n.plane] = (cut[n.plane] || 0) + 1; return false; }
      if (areas.indexOf(a) < 0) areas.splice(areas.indexOf(base) + k2 - 1, 0, a);
      n.area = a; cells[n.plane + ':' + a] = (cells[n.plane + ':' + a] || 0) + 1; nodes.push(n); return true;
    };
    var turnId = function (mid) { return 'turn:' + mid; };
    var turns = (d.turns || []).slice(-CELL), haveTurn = {};
    turns.forEach(function (t) { if (put({ id: turnId(t.mid), label: String(t.label || t.mid).slice(0, 40), plane: 'chat', area: AREAS[0], kind: 'client', detail: 'turn ' + t.mid, desk: { turn: t.mid } })) haveTurn[t.mid] = 1; });
    (d.items || []).forEach(function (it) {
      var layered = it.layer === 'behind' || it.layer === 'front';
      var n = { id: String(it.key), label: String(it.label || it.key).slice(0, 40), plane: layered ? 'panel' : 'items', area: AREAS[areaOfFace(it.face)],
                kind: it.type === 'panel' ? 'client' : /records|table|fabric/.test(String(it.type || '')) ? 'store' : 'core',
                detail: String(it.type || 'item') + (it.face ? ' · ' + it.face : ''), desk: { key: String(it.key), face: it.face || '', layer: it.layer || '' } };
      if (put(n) && it.mid && haveTurn[it.mid]) links.push({ a: n.id, b: turnId(it.mid), kind: 'req' });
    });
    var focus = d.focusMid && haveTurn[d.focusMid] ? turnId(d.focusMid) : (turns.length ? turnId(turns[turns.length - 1].mid) : null);
    (d.graph || []).forEach(function (g, i) {
      var n = { id: 'ctx:' + g.id, label: String(g.label || g.id).slice(0, 40), plane: 'graph', area: AREAS[Math.min(2, Math.floor(i / CELL))], kind: 'store', detail: 'in the context', desk: { ctx: g.id } };
      if (put(n) && focus) links.push({ a: n.id, b: focus, kind: 'mem' });
    });
    var name = { chat: 'Chat', items: 'Desk items', panel: 'UI panel', graph: 'Context graph' };
    var planes = ['chat', 'items', 'panel', 'graph'].map(function (id) { return { id: id, name: name[id] + (cut[id] ? ' (+' + cut[id] + ' more)' : '') }; });
    return { planes: planes, areas: areas, nodes: nodes, links: links };
  }

  var api = { DESK: DESK, layerOrder: layerOrder, chatRect: chatRect, snapEdge: snapEdge, distance: distance, sizeAt: sizeAt, faceOf: faceOf,
              arrange: arrange, pushOut: pushOut, separate: separate, stackOrder: stackOrder, fullRegion: fullRegion, makeRoom: makeRoom,
              latticeOf: latticeOf };
  root.VeraDesk = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
