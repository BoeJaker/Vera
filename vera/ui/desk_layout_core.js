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

  /* THE CARDS TAKE THE ROOM AROUND THEM (owner, 2026-10-10: "for things on the desk to take up as much space as possible
     and as good design allows"). After the cards are placed and pulled apart, each one that may grow (not a chip, not
     fixed: the person's size, a lock and the card being sized stay as they are) grows into the free desk round it - a
     step at a time, right, down, left and up in turn, while a side stays clear of the chat, the desk's edge and every
     other card by `gap` - until no side can. Its width and height keep within an aspect of 1:2.2 either way, so a
     card grows into a page, never a ribbon (an item's own `ratio` - a chart's, a list's - widens that); `maxW` / `maxH` cap it, and an item's own maxW / maxH (what it holds). Order is the items' order: the first (the topic's)
     grow first. Items are {x, y, w, h (centre and size), fixed?, chip?}; each grown one gets grown: true. */
  function grow(items, R, d, o) {
    d = desk(d); o = o || {};
    var gap = o.gap || 14, step = o.step || 8, ratio = o.ratio || 2.2;
    var maxW = o.maxW || d.W - 16, maxH = o.maxH || d.H - 8;
    function box(it) { return { l: it.x - it.w / 2, t: it.y - it.h / 2, r: it.x + it.w / 2, b: it.y + it.h / 2 }; }
    function hit(a, b, g) { return a.l < b.r + g && b.l < a.r + g && a.t < b.b + g && b.t < a.b + g; }
    var chat = R && R.r > R.l ? R : null;
    items.forEach(function (it) {
      if (it.fixed || it.chip || it.own) return;   /* own: a size the person gave - kept, never grown (WSU-74) */
      var others = items.filter(function (x) { return x !== it; });
      /* the gap is kept in the direction it grows; across it only a hairline, or a neighbour 12 px to the side would stop it
         growing up or down at all */
      function hit2(a, q, gx, gy) { return a.l < q.r + gx && q.l < a.r + gx && a.t < q.b + gy && q.t < a.b + gy; }
      function clear(b, s) {
        if (b.l < 0 || b.t < 0 || b.r > d.W || b.b > d.H) return false;
        var gx = s === 'r' || s === 'l' ? gap : 4, gy = s === 'b' || s === 't' ? gap : 4;
        if (chat && hit2(b, chat, gx, gy)) return false;
        for (var i = 0; i < others.length; i++) if (hit2(b, box(others[i]), gx, gy)) return false;
        return true;
      }
      /* only the strip a side grows into is tested: cards the arrangement set closer than `gap` (its bands are 12 px
         apart) are not blocked by the room they already have */
      var b = box(it);
      /* a side is closed by a neighbour, an edge or the cap; the aspect only holds it back for a round (it opens again
         once the other way has grown). Done when a round grows nothing. */
      var open = { r: true, b: true, l: true, t: true }, n = 0, moved = true;
      while (moved && n++ < 800) {
        moved = false;
        ['r', 'b', 'l', 't'].forEach(function (s) {
          if (!open[s]) return;
          var t = { l: b.l, t: b.t, r: b.r, b: b.b }; t[s] += (s === 'r' || s === 'b') ? step : -step;
          var w = t.r - t.l, h = t.b - t.t;
          var strip = s === 'r' ? { l: b.r, t: b.t, r: t.r, b: b.b } : s === 'l' ? { l: t.l, t: b.t, r: b.l, b: b.b }
            : s === 'b' ? { l: b.l, t: b.b, r: b.r, b: t.b } : { l: b.l, t: t.t, r: b.r, b: b.t };
          if (w > Math.min(maxW, it.maxW || Infinity) || h > Math.min(maxH, it.maxH || Infinity) || !clear(strip, s)) { open[s] = false; return; }
          var rt = it.ratio || ratio;   /* a kind's own aspect: a chart may be wider, a list taller, than a page */
          if (w / h > rt || h / w > rt) return;
          b = t; moved = true;
        });
      }
      var nw = b.r - b.l, nh = b.b - b.t;
      if (nw > it.w + 1 || nh > it.h + 1) { it.x = b.l + nw / 2; it.y = b.t + nh / 2; it.w = nw; it.h = nh; it.grown = true; }
    });
    return items;
  }

  /* THE CARDS SIT UP THE DESK (owner, 2026-10-11: "desk elements seem to not use the entire height of the desk and drop in
     in the middle ... they could be further up the desk"): the bands are dealt round the chat's middle, and a card fitted
     to what it holds left the room above it empty. After they are pulled apart, each card that may move (not fixed: the
     person's place, a lock, the card being sized) rises, top-most first, until it meets a card above it, the chat or the
     top of the desk, keeping `gap`. Items are {x, y, w, h (centre and size), fixed?}; a moved one gets settled: true. */
  function settle(items, R, d, o) {
    d = desk(d); o = o || {};
    var gap = o.gap == null ? 12 : o.gap, step = o.step || 4, top = o.top == null ? 4 : o.top;
    var chat = R && R.r > R.l ? R : null;
    /* the gap is kept above and below; beside, a hairline - the next band is 12 px away and must not stop a card rising */
    function hit(a, b) { return a.x - a.w / 2 < b.x + b.w / 2 + 2 && b.x - b.w / 2 < a.x + a.w / 2 + 2 && a.y - a.h / 2 < b.y + b.h / 2 + gap && b.y - b.h / 2 < a.y + a.h / 2 + gap; }
    function inChat(a) { return chat && a.x + a.w / 2 > chat.l - gap && a.x - a.w / 2 < chat.r + gap && a.y + a.h / 2 > chat.t - gap && a.y - a.h / 2 < chat.b + gap; }
    var order = items.slice().sort(function (a, b) { return (a.y - a.h / 2) - (b.y - b.h / 2); });
    order.forEach(function (it) {
      if (it.fixed || it.stay) return;   /* stay: put there by the person's hand - it stays where it was let go */
      var others = items.filter(function (x) { return x !== it; }), y0 = it.y, n = 0;
      while (it.y - it.h / 2 - step >= top && n++ < 2000) {
        var t = { x: it.x, y: it.y - step, w: it.w, h: it.h };
        if (inChat(t) || others.some(function (b) { return hit(t, b); })) break;
        it.y = t.y;
      }
      if (it.y !== y0) it.settled = true;
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
    var CELL = 9, nodes = [], links = [], cells = {}, areas = AREAS.slice();
    var areaOfFace = function (f) { return f === 'max' || f === 'layer-front' || f === 'layer-behind' ? 0 : f === 'card' ? 1 : 2; };
    var put = function (n) {
      var base = n.area, a = base, k2 = 1;
      while ((cells[n.plane + ':' + a] || 0) >= CELL) { k2++; a = base + ' · ' + k2; }
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
    var planes = ['chat', 'items', 'panel', 'graph'].map(function (id) { return { id: id, name: name[id] }; });
    return { planes: planes, areas: areas, nodes: nodes, links: links };
  }


  /* ── THE DESK'S MODES (DESK-ARRANGEMENT DR-01..03; owner decision D2, 2026-10-16: "replace with new modes") ──────────
     orbit / by kind / by time / stack barely differed (most items were border pills in every one). The modes now lay the
     desk out by the situation: auto (the topic's items in bands out from the chat - the old orbit), focus (one large, its
     relations round it), compare (2-4 of a kind side by side, the same size, tops aligned), flow (in the order the work
     happened), board (columns by status), map (clusters of what shares context), free (the person's places; automation
     only fills holes). Items: [{id, kind, rel:[keys], status, t, border}] in the desk's order, the most relevant first.
     A layout returns {cells:{id:{x,y,w,h}} (centre and size), chips:[id], lanes:[{x,y,w,h,label}]}. */
  var MODES = ['auto', 'focus', 'compare', 'flow', 'board', 'map', 'free', 'slots'];
  var LEGACY_MODE = { orbit: 'auto', stack: 'auto', kind: 'map', timeline: 'flow' };
  function modeOf(m) { m = String(m || ''); if (LEGACY_MODE[m]) return LEGACY_MODE[m]; return MODES.indexOf(m) >= 0 ? m : 'auto'; }

  /* the free part of the desk: beside the chat on its roomier side, short of the pill strip at the far end */
  function freeRegion(R, d, strip) {
    d = desk(d); var g = 14, s = strip == null ? SIZE.chip.w + 20 : strip, top = d.top == null ? 4 : d.top;
    if (R.t > d.H * 0.55 && R.l < d.W * 0.5 && R.r > d.W * 0.5) return { l: d.m, t: top, r: d.W - d.m - s, b: R.t - g, farRight: true };
    var leftRoom = R.l - d.m, rightRoom = d.W - d.m - R.r;
    if (rightRoom >= leftRoom) return { l: Math.max(d.m, R.r + g), t: top, r: d.W - d.m - s, b: d.H - 8, farRight: true };
    return { l: d.m + s, t: top, r: Math.min(d.W - d.m, R.l - g), b: d.H - 8, farRight: false };
  }
  function cellAt(l, t, w, h) { return { x: l + w / 2, y: t + h / 2, w: Math.round(w), h: Math.round(h) }; }
  function shares(a, b) { var ra = (a && a.rel) || [], rb = (b && b.rel) || []; for (var i = 0; i < ra.length; i++) if (rb.indexOf(ra[i]) >= 0) return true; return false; }

  function layoutMode(mode, R, items, d, o) {
    d = desk(d); o = o || {}; mode = modeOf(mode);
    var out = { cells: {}, chips: [], lanes: [] };
    if (mode === 'auto') return null;
    var F = freeRegion(R, d, o.strip), fw = Math.max(0, F.r - F.l), fh = Math.max(0, F.b - F.t), g = 12;
    var live = items.filter(function (it) { return it && !it.border; });
    items.forEach(function (it) { if (it && it.border) out.chips.push(it.id); });
    var used = {}; var put = function (it, c) { out.cells[it.id] = c; used[it.id] = true; };
    var rest = function () { live.forEach(function (it) { if (!used[it.id]) out.chips.push(it.id); }); return out; };
    if (!live.length || fw < 160 || fh < 90) { live.forEach(function (it) { out.chips.push(it.id); }); return out; }
    if (mode === 'free') { out.free = true; return out; }
    /* SLOTS (LV-2/3, DESK-ARRANGEMENT 7): fitLayout over the free desk - each item's `aspect` and its place in the order as its rank;
       the front of each slot is a cell, the rest of a slot (its composite tabs, grouped by the canvas before this) and the pills chips */
    if (mode === 'slots') { var L = fitLayout(live.map(function (it, i) { return { id: it.id, aspect: it.aspect || 'square', rank: it.rank != null ? it.rank : i }; }), F, { near: F.farRight ? 'left' : 'right', current: o.current || {}, noComposite: o.noComposite });
      L.slots.forEach(function (s) { var id = s.ids[0]; out.cells[id] = cellAt(s.l, s.t, s.w, s.h); used[id] = true; s.ids.slice(1).forEach(function (m) { out.chips.push(m); used[m] = true; }); });
      L.pills.forEach(function (id) { out.chips.push(id); used[id] = true; }); out.fit = L; return rest(); }

    if (mode === 'focus') {
      var lead = live[0]; var leadW = Math.round(fw * 0.6), leadH = Math.round(Math.min(fh, leadW * 0.85));
      put(lead, cellAt(F.farRight ? F.l : F.r - leadW, F.t, leadW, leadH));
      var sat = live.slice(1).filter(function (it) { return shares(it, lead); }).slice(0, 6);
      var colW = fw - leadW - g;
      if (colW >= 200 && sat.length) { var hh = Math.min(260, (fh - g * (sat.length - 1)) / sat.length);
        if (hh >= 80) sat.forEach(function (it, i) { put(it, cellAt(F.farRight ? F.l + leadW + g : F.l, F.t + i * (hh + g), colW, hh)); }); }
      return rest();
    }
    if (mode === 'compare') {
      var k0 = live[0].kind; var set = live.filter(function (it) { return it.kind === k0; }).slice(0, 4);
      if (set.length < 2) set = live.slice(0, Math.min(4, live.length));
      var n = set.length, cols = fw / n >= 260 ? n : Math.min(2, n), rows = Math.ceil(n / cols);
      var w = (fw - g * (cols - 1)) / cols, h = Math.min((fh - g * (rows - 1)) / rows, w * 0.9);
      set.forEach(function (it, i) { put(it, cellAt(F.l + (i % cols) * (w + g), F.t + Math.floor(i / cols) * (h + g), w, h)); });
      return rest();
    }
    if (mode === 'flow') {
      var ord = live.slice().sort(function (a, b) { return (a.t || 0) - (b.t || 0); });
      var c3 = Math.max(1, Math.min(3, Math.floor((fw + g) / (280 + g)))); var fwc = (fw - g * (c3 - 1)) / c3, fhc = Math.min(fwc * 0.75, 300);
      var rws = Math.max(1, Math.floor((fh + g) / (fhc + g))), cap = c3 * rws; var shown = ord.length > cap ? ord.slice(-cap) : ord;
      shown.forEach(function (it, i) { put(it, cellAt(F.l + (i % c3) * (fwc + g), F.t + Math.floor(i / c3) * (fhc + g), fwc, fhc)); });
      return rest();
    }
    if (mode === 'board') {
      var LANES = [['todo', 'To do'], ['running', 'Running'], ['needs', 'Needs you'], ['done', 'Done']], HD = 22;
      var lw = (fw - g * 3) / 4;
      LANES.forEach(function (ln, li) { var x0 = F.l + li * (lw + g); out.lanes.push({ x: x0, y: F.t, w: lw, h: HD, label: ln[1], n: 0 });
        var its = live.filter(function (it) { return (it.status || 'todo') === ln[0]; }); out.lanes[li].n = its.length;
        var k = its.length; if (!k) return; var hb = Math.min(200, (fh - HD - g * (k - 1)) / k);
        if (hb < 80) { var fit = Math.max(0, Math.floor((fh - HD + g) / (80 + g))); its.slice(0, fit).forEach(function (it, i) { put(it, cellAt(x0, F.t + HD + i * (80 + g), lw, 80)); }); return; }
        its.forEach(function (it, i) { put(it, cellAt(x0, F.t + HD + i * (hb + g), lw, hb)); }); });
      return rest();
    }
    if (mode === 'map') {
      /* clusters: items joined by a shared key, biggest first; each a cell of the desk, its items a small grid in it */
      var par = live.map(function (_, i) { return i; }); var find = function (i) { while (par[i] !== i) { par[i] = par[par[i]]; i = par[i]; } return i; };
      /* a key most items hold (the topic, one long turn) tells nothing apart: clusters are made of the keys that do */
      var freq = {}; live.forEach(function (it) { (it.rel || []).forEach(function (k) { freq[k] = (freq[k] || 0) + 1; }); });
      var common = function (k) { return freq[k] > Math.max(2, live.length * 0.6); };
      var keysOf = function (it) { return (it.rel || []).filter(function (k) { return !common(k); }); };
      for (var a = 0; a < live.length; a++) for (var b = a + 1; b < live.length; b++) if (shares({ rel: keysOf(live[a]) }, { rel: keysOf(live[b]) })) par[find(a)] = find(b);
      var cl = {}; live.forEach(function (it, i) { var r = find(i); (cl[r] = cl[r] || []).push(it); });
      var cls = Object.keys(cl).map(function (k) { return cl[k]; }).sort(function (x, y) { return y.length - x.length; });
      var cc = Math.ceil(Math.sqrt(cls.length)), cr = Math.ceil(cls.length / cc), HD2 = 20;
      var cw = (fw - g * (cc - 1)) / cc, ch = (fh - g * (cr - 1)) / cr;
      cls.forEach(function (list, ci) { var x0 = F.l + (ci % cc) * (cw + g), y0 = F.t + Math.floor(ci / cc) * (ch + g);
        var key = ''; keysOf(list[0]).some(function (k) { if (list.every(function (it) { return (it.rel || []).indexOf(k) >= 0; })) { key = k; return true; } return false; });
        out.lanes.push({ x: x0, y: y0, w: cw, h: HD2, label: (key || list[0].kind || '').replace(/^[a-z]+:/, '').slice(0, 40), n: list.length });
        /* the grid of the cluster's cards that gives each the most room, then no taller than 1.2 x its width */
        var k = list.length, gc = 1, best = -1; for (var q = 1; q <= k; q++) { var r0 = Math.ceil(k / q), w0 = (cw - 8 * (q - 1)) / q, h0 = (ch - HD2 - 8 * (r0 - 1)) / r0, sc0 = Math.min(w0, h0 * 1.5); if (sc0 > best) { best = sc0; gc = q; } }
        var gr = Math.ceil(k / gc), iw = (cw - 8 * (gc - 1)) / gc, ih = Math.min((ch - HD2 - 8 * (gr - 1)) / gr, iw * 1.2);
        if (iw < 150 || ih < 70) return;
        list.forEach(function (it, i) { put(it, cellAt(x0 + (i % gc) * (iw + 8), y0 + HD2 + Math.floor(i / gc) * (ih + 8), iw, ih)); }); });
      return rest();
    }
    return null;
  }

  /* THE PLACE FOR ONE MORE (DR-01): the free slot a new item fits best - 3 x relation (how many of its neighbours share its
     keys) + 2 x attention (nearness to the chat) + 1 x fit (its size, or three quarters of it) - nothing displaced, as only
     free room is a candidate. placed: [{x,y,w,h,rel}] (centres). Returns {x,y,w,h,score} or null. */
  function bestSlot(item, placed, R, d, o) {
    d = desk(d); o = o || {}; var F = o.region || freeRegion(R, d, o.strip), g = 10, step = o.step || 24;
    var hit = function (a, b) { return a.l < b.r + g && b.l < a.r + g && a.t < b.b + g && b.t < a.b + g; };
    var boxes = (placed || []).map(function (p) { return { l: p.x - p.w / 2, t: p.y - p.h / 2, r: p.x + p.w / 2, b: p.y + p.h / 2, rel: p.rel || [] }; });
    var chat = { l: R.l, t: R.t, r: R.r, b: R.b }, best = null;
    [1, 0.75].forEach(function (k) { if (best) return; var w = Math.round(item.w * k), h = Math.round(item.h * k);
      for (var y = F.t; y + h <= F.b; y += step) for (var x = F.l; x + w <= F.r; x += step) {
        var c = { l: x, t: y, r: x + w, b: y + h }; if (hit(c, chat) || boxes.some(function (b) { return hit(c, b); })) continue;
        var cx = x + w / 2, cy = y + h / 2, reach = 1.5 * Math.max(w, h);
        /* each neighbour sharing its keys counts by how close it is: next to it is worth more than across the desk */
        var rel = Math.min(1, boxes.reduce(function (s, b) { if (!shares({ rel: b.rel }, item)) return s; var dd = Math.hypot((b.l + b.r) / 2 - cx, (b.t + b.b) / 2 - cy); return s + Math.max(0, 1 - dd / reach); }, 0));
        var att = clamp(1 - distance(R, cx, cy, d), 0, 1);
        var s = 3 * rel + 2 * att + k;
        if (!best || s > best.score) best = { x: cx, y: cy, w: w, h: h, score: +s.toFixed(3) };
      } });
    return best;
  }

  /* PRE-TURN ITEMS LEAVE FOCUS FIRST (LV-9 / WSU-87, owner 2026-10-17: "some elements are added before the llm returns anything
     - they seem to be a bit more sticky ... they should probably move out of focus the fastest"). A pre-turn item is one the
     chat put on the canvas AROUND the reply, not one the reply made: what turn.prepare placed before the answer (anchor from
     'prepare', keys enrich:... or pre:panel:...) and what the enrichment pass brings after it (from 'enrich', keys enrich:...).
       preTurn(key, from)              is it one
       topicOrder(keys, score, isPre)  the topic's keys best first - every reply item before every pre-turn item, so a topic
                                       with more items than the room beside the chat drops the pre-turn ones first
       preTurnOut(items, keys, mid)    the pre-turn items the topic has moved past: [key]. items: [{key, from, anchors:[mid],
                                       pinned, locked, kept}]. In the topic, or made for the turn in focus (one of its anchors
                                       is mid), it stays; pinned, locked (the person said "do not move") or kept (opened again
                                       by the person under this topic) it stays; any other goes - its place by hand
                                       (place.by 'person') included: that place is where it sits WHEN it is in focus, and is
                                       kept for then, but it does not hold the item in focus. */
  function preTurn(key, from) { key = String(key || ''); from = String(from || '');
    return key.indexOf('enrich:') === 0 || key.indexOf('pre:') === 0 || from === 'enrich' || from === 'prepare'; }
  function topicOrder(keys, score, isPre) {
    var sc = typeof score === 'function' ? score : function () { return 0; }, pre = typeof isPre === 'function' ? isPre : function (k) { return preTurn(k); };
    return (keys || []).map(function (k, i) { k = String(k); return { k: k, i: i, p: pre(k) ? 1 : 0, s: +sc(k) || 0 }; })
      .sort(function (a, b) { return (a.p - b.p) || (b.s - a.s) || (a.i - b.i); }).map(function (x) { return x.k; });
  }
  function preTurnOut(items, keys, mid) {
    var inT = {}; (keys || []).forEach(function (k) { inT[String(k)] = 1; }); mid = String(mid || '');
    return (items || []).filter(function (x) { if (!x || !x.key || !preTurn(x.key, x.from)) return false;
      if (inT[String(x.key)] || x.pinned || x.locked || x.kept) return false;
      return !(mid && (x.anchors || []).map(String).indexOf(mid) >= 0); }).map(function (x) { return String(x.key); });
  }


  /* LAYOUT v2: SLOTS BY ASPECT (DESK-ARRANGEMENT 7, LV-2; owner 2026-10-17: "define some layouts that can be used to construct any
     type of layout and try to fit to one of these layouts"; "when the layout runs out of space, instead of making each element in
     the layout smaller they can composite in their preferred aspect").

     fitLayout(items, region, o) -> { name, slots: [{ id, aspect, l, t, w, h, ids: [front, ...behind] }], pills: [ids], score }
       items   [{ id, aspect: 'portrait'|'landscape'|'square'|'tile', rank (0 = most relevant) }]
       region  { l, t, r, b } - the free desk (the chat and the pill strip already taken out)
       o       { gap, current: { id: { x, y, w, h } (centre and size) }, near: 'left'|'right' (the chat's side), noComposite: { id: 1 } }

     One recipe builds every layout: portrait COLUMNS down one side, and the rest in ROWS - landscape BANDS, a row of SQUARES, a
     GRID of tiles. The candidates differ in how many columns, which side, and whether the bands go above or below. Each item takes
     a slot of its aspect, most relevant first; when its aspect has no free slot it JOINS the least full slot of that aspect (a
     composite) rather than every slot shrinking; tiles join last (they would rather be pills). The best candidate keeps the most
     items in slots of their own, makes the fewest composites and pills, and - when the desk already shows something (o.current) -
     moves the fewest cards. Pure: no DOM. */
  var ASPECT_SIZE = { portrait: { w: 460, minW: 340, maxW: 560, minH: 320 }, landscape: { minW: 480, h: 360, minH: 240, maxH: 600 },
    square: { w: 500, h: 400, minW: 300, minH: 220, maxW: 720, maxH: 560 }, tile: { w: 280, h: 180 } };
  function fitLayout(items, region, o) {
    o = o || {}; var G = o.gap == null ? 10 : o.gap, cur = o.current || {};
    var reg = { l: region.l, t: region.t, w: Math.max(0, region.r - region.l), h: Math.max(0, region.b - region.t) };
    var by = { portrait: [], landscape: [], square: [], tile: [] };
    (items || []).slice().sort(function (a, b) { return (a.rank || 0) - (b.rank || 0); }).forEach(function (it) { (by[it.aspect] || by.square).push(it); });
    var plans = [], sides = o.near === 'right' ? ['right', 'left'] : ['left', 'right'];
    /* columns: with nothing but lists they may take the whole desk; with other things, about 3/5 of it */
    var onlyP = by.landscape.length + by.square.length + by.tile.length === 0;
    var maxCols = Math.max(0, Math.min(by.portrait.length, onlyP ? 4 : 3, Math.floor(((onlyP ? reg.w : reg.w * 0.62) + G) / (ASPECT_SIZE.portrait.minW + G))));
    for (var np = maxCols; np >= 0; np--) {
      for (var si = 0; si < (np ? 2 : 1); si++) for (var lp = 0; lp < 2; lp++) { var pl0 = build(np, sides[si], lp ? 'bottom' : 'top'); if (pl0) plans.push(pl0); }
    }
    function build(np, side, lpos) {
      var slots = [], P = ASPECT_SIZE.portrait;
      var pw = np ? Math.max(P.minW, Math.min(P.maxW, Math.floor((reg.w - G * np) * (np === 1 ? 0.36 : 0.62) / np))) : 0;
      if (onlyP && np) pw = Math.min(P.maxW, Math.floor((reg.w - G * (np - 1)) / np));
      if (np) { pw = Math.min(pw, Math.floor((reg.w - G * (np - 1)) / np)); if (pw < P.minW - 60) return null; }
      var colsW = np ? np * pw + (np - 1) * G : 0;
      for (var c = 0; c < np; c++) slots.push({ aspect: 'portrait', l: side === 'left' ? reg.l + c * (pw + G) : reg.l + reg.w - colsW + c * (pw + G), t: reg.t, w: pw, h: reg.h });
      var rx = side === 'left' ? reg.l + colsW + (np ? G : 0) : reg.l, rw = reg.w - colsW - (np ? G : 0);
      if (rw >= 240) {
        var nL = Math.min(by.landscape.length, 2), nS = by.square.length, nT = by.tile.length;
        var perS = nS ? Math.max(1, Math.min(nS, Math.floor((rw + G) / (ASPECT_SIZE.square.minW + G)))) : 0;
        var tw = Math.min(ASPECT_SIZE.tile.w, rw);
        var perT = nT ? Math.max(1, Math.floor((rw + G) / (tw + G))) : 0, rowsT = nT ? Math.min(2, Math.ceil(nT / perT)) : 0;
        /* a band is wide: no taller than its width / 1.6 (and 600) */
        var maxBand = Math.min(ASPECT_SIZE.landscape.maxH, Math.floor(rw / 1.6));
        var hT = rowsT ? rowsT * ASPECT_SIZE.tile.h + (rowsT - 1) * G : 0, hS = perS ? ASPECT_SIZE.square.h : 0, hL = nL ? Math.min(ASPECT_SIZE.landscape.h, maxBand) * nL + (nL - 1) * G : 0;
        /* too tall for the desk: the rows shrink together, and a row shrunk past use drops out (tiles first, then squares, then a
           second band) - its items go to the next aspect's slots, or join them */
        var gaps = function () { return Math.max(0, (hT ? 1 : 0) + (hS ? 1 : 0) + (hL ? 1 : 0) - 1) * G; };
        for (var guard = 0; guard < 6; guard++) { var sum = hT + hS + hL; if (sum + gaps() <= reg.h) break; var k = (reg.h - gaps()) / Math.max(1, sum);
          if (hT && hT * k < 110 * rowsT) { if (rowsT > 1) { rowsT = 1; hT = ASPECT_SIZE.tile.h; } else { hT = 0; rowsT = 0; } continue; }
          if (hS && hS * k < ASPECT_SIZE.square.minH) { hS = 0; perS = 0; continue; }
          if (hL && nL > 1 && hL * k / nL < ASPECT_SIZE.landscape.minH) { nL = 1; hL = Math.min(ASPECT_SIZE.landscape.h, maxBand); continue; }
          hT = Math.floor(hT * k); hS = Math.floor(hS * k); hL = Math.floor(hL * k); break; }
        var spare = reg.h - (hT + hS + hL + gaps());
        if (hL) { var gl = Math.max(0, Math.min(spare, maxBand * nL + (nL - 1) * G - hL)); hL += gl; spare -= gl; }
        if (hS) { var gs = Math.max(0, Math.min(spare, ASPECT_SIZE.square.maxH - hS)); hS += gs; spare -= gs; }
        var rows = []; if (hT) rows.push(['tile', hT]); if (hS) rows.push(['square', hS]); if (hL) rows[lpos === 'top' ? 'unshift' : 'push'](['landscape', hL]);
        var y = reg.t;
        rows.forEach(function (row) { var a = row[0], h = row[1];
          if (a === 'landscape') { var bh = Math.floor((h - (nL - 1) * G) / nL); for (var i = 0; i < nL; i++) slots.push({ aspect: a, l: rx, t: y + i * (bh + G), w: rw, h: bh }); }
          else if (a === 'square') { var sw = Math.min(ASPECT_SIZE.square.maxW, Math.floor((rw - (perS - 1) * G) / perS)); for (var j = 0; j < perS; j++) slots.push({ aspect: a, l: rx + j * (sw + G), t: y, w: sw, h: h }); }
          else { var th = Math.floor((h - (rowsT - 1) * G) / rowsT); for (var q = 0; q < rowsT * perT; q++) slots.push({ aspect: a, l: rx + (q % perT) * (tw + G), t: y + Math.floor(q / perT) * (th + G), w: tw, h: th }); }
          y += h + G; });
      }
      slots.forEach(function (sl, i) { sl.id = sl.aspect[0] + i; sl.ids = []; });
      /* items to slots: their own aspect first; with none free, the least full slot of their aspect (tiles last); with no slot of
         their aspect at all, the nearest one (portrait -> square -> landscape; tile -> square) */
      var pills = [], ALT = { portrait: ['square', 'landscape'], landscape: ['square', 'portrait'], square: ['landscape', 'portrait'], tile: ['square'] };
      ['landscape', 'portrait', 'square', 'tile'].forEach(function (a) { by[a].forEach(function (it) {
        var own = slots.filter(function (s) { return s.aspect === a; }), free = own.filter(function (s) { return !s.ids.length; });
        if (free.length) { free.sort(function (p, q2) { return slotCost(it, p) - slotCost(it, q2); }); free[0].ids.push(it.id); return; }
        /* a composite is always of one aspect: another aspect's slot is taken only while it is FREE (a list never a tab of a table) */
        if (!own.length) { for (var z = 0; z < ALT[a].length; z++) { var alt = slots.filter(function (s) { return s.aspect === ALT[a][z] && !s.ids.length; }); if (alt.length) { alt[0].ids.push(it.id); alt[0].aspectOf = a; return; } } pills.push(it.id); return; }
        /* a card holds at most o.maxTabs (8) tabs - past that, a pill of its own (a ninth was split off as a card and took the column) */
        var room = own.filter(function (s) { return s.ids.length < (o.maxTabs || 8); });
        if (!room.length || (o.noComposite && o.noComposite[it.id]) || (a === 'tile' && own.length < 2)) { pills.push(it.id); return; }
        room.sort(function (p, q2) { return p.ids.length - q2.ids.length || slotCost(it, p) - slotCost(it, q2); }); room[0].ids.push(it.id); }); });
      var used = slots.filter(function (s) { return s.ids.length; });
      return { name: (np ? np + ' column' + (np > 1 ? 's' : '') + ' ' + side : 'rows') + ', bands ' + lpos, slots: used, pills: pills, empty: slots.length - used.length };
    }
    /* how far a slot is from where the item is now (0 when it is new) */
    function slotCost(it, s) { var c0 = cur[it.id]; if (!c0) return 0; var cx = s.l + s.w / 2, cy = s.t + s.h / 2;
      return Math.hypot(cx - c0.x, cy - c0.y) / 200 + (Math.abs(s.w - c0.w) + Math.abs(s.h - c0.h)) / 400; }
    var best = null;
    plans.forEach(function (pl) {
      var score = 0;
      pl.slots.forEach(function (s) { s.ids.forEach(function (id, i) { var it = (items || []).find(function (x) { return x.id === id; }) || {}; var wgt = 1 / (1 + (it.rank || 0) * 0.25);
        score += i === 0 ? 10 * wgt : -4 * wgt; score -= 2 * slotCost(it, s); }); });
      score -= 12 * pl.pills.length + 0.5 * pl.empty;
      pl.score = Math.round(score * 100) / 100;
      if (!best || pl.score > best.score) best = pl; });
    return best || { name: 'none', slots: [], pills: (items || []).map(function (x) { return x.id; }), score: 0 };
  }

  var api = { DESK: DESK, grow: grow, MODES: MODES, modeOf: modeOf, freeRegion: freeRegion, layoutMode: layoutMode, bestSlot: bestSlot, settle: settle, layerOrder: layerOrder, chatRect: chatRect, snapEdge: snapEdge, distance: distance, sizeAt: sizeAt, faceOf: faceOf,
              arrange: arrange, pushOut: pushOut, separate: separate, stackOrder: stackOrder, fullRegion: fullRegion, makeRoom: makeRoom,
              latticeOf: latticeOf, preTurn: preTurn, topicOrder: topicOrder, preTurnOut: preTurnOut, fitLayout: fitLayout, ASPECT_SIZE: ASPECT_SIZE };
  root.VeraDesk = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
