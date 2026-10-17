/* VeraItem - the item contract (DEEP-29 section 11A.4), pure: the same in the browser (window.VeraItem) and in Node.

   An item is one thing on a surface (desk/canvas, dashboard grid, LHM slot, chat reply, notebook), drawn as ONE container
   (head + controls + resize + faces) around ONE body. This file is the browser half of the record and the body registry:

     normalise(block)              the item record of a stored block - the same answers as vera/canvas/item_core.py
     applySet(block, fields)       {changed, prev, problems} for a canvas.item.set call, applied in place when valid
     faceOf(block)                 pill | tile | window | full (a block with no face has the one its state implies)
     registerBody(kind, spec)      a body declares {draw, views, fit, minSize, actions}; the container draws the head
     body(kind)                    the declared spec, or the default one (one view, fit 'content', minSize 's')
     controls(kind, o)             the head's standard row for a kind, in the ADR's order: view, lock, pill, max, more, drop;
                                   o.narrow (<320 px) keeps lock, pill, more, drop
     menu(kind, item)              the more-menu: the standard entries, then the body's own actions
     Z                             the z-order bands (vera-item.css carries the same table as custom properties)
     z(band, n)                    the z-index for a band; tiles and windows take an offset n inside their range

   Bodies never draw a head. A kind that is not registered still gets the full container. */
(function (root) {
  'use strict';

  var STATES = ['now', 'pinned', 'parked', 'hidden'];
  var FACES = ['pill', 'tile', 'window', 'full'];
  var SIZES = ['xs', 's', 'm', 'l', 'xl'];
  var LAYERS = ['among', 'behind', 'front'];
  var PLACED_BY = ['auto', 'person'];
  var VIEWS = ['list', 'cards', 'table', 'graph', 'chart', 'chips', 'fields', 'code', 'structure', 'both', 'tree', 'flat', 'validate', 'drawn'];   /* = item_core.VIEWS (WSU-58 S1) */
  var SETTABLE = ['face', 'place', 'view', 'size', 'locked', 'folded', 'group', 'state'];
  var PLACE_NUM = { fx: [0, 1], fy: [0, 1], w: [34, 8000], h: [24, 8000], z: [0, 999] };

  /* Decision 6: one z table. A tile's z rises with its closeness to the chat (10-110); a window's with how recently it
     was focused (130-149). Every item host reads these and only these. */
  var Z = { back: 1, layerBehind: 2, tile: [10, 110], pill: 115, tileFocus: 120, window: [130, 149], full: 150,
            layerFront: 155, flash: 180, drag: 200, pop: 230, menu: 240, chat: 250, sheet: 300 };

  function has(a, v) { return a.indexOf(v) >= 0; }
  function num(v) { if (v === null || v === undefined || v === '' || typeof v === 'boolean') return null; var f = Number(v); return isFinite(f) ? f : null; }
  function isObj(v) { return !!v && typeof v === 'object' && !Array.isArray(v); }
  function clone(o) { var r = {}; for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) r[k] = o[k]; return r; }
  function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  /* Python's round(): halves go to the even neighbour - the two halves must agree on every place they store */
  function pyRound(x, nd) { var m = Math.pow(10, nd || 0), v = x * m, f = Math.floor(v), d = v - f;
    var r = d > 0.5 + 1e-9 ? f + 1 : d < 0.5 - 1e-9 ? f : (f % 2 === 0 ? f : f + 1); return r / m; }

  function z(band, n) {
    var b = Z[band]; if (b === undefined) return Z.tile[0];
    if (Array.isArray(b)) { n = Math.max(0, Math.round(n || 0)); return Math.min(b[1], b[0] + n); }
    return b;
  }

  function faceOf(block) {
    var f = String((block && block.face) || '');
    if (has(FACES, f)) return f;
    return block && block.state === 'parked' ? 'pill' : 'tile';
  }

  function normPlace(p, prev) {
    if (p === null || p === undefined) return { place: null, bad: [] };
    if (!isObj(p)) return { place: null, bad: ['place must be an object {fx, fy, w, h, by, locked, layer, z}'] };
    var out = isObj(prev) ? clone(prev) : {}, bad = [];
    Object.keys(PLACE_NUM).forEach(function (k) {
      if (!(k in p)) return;
      var n = num(p[k]); if (n === null) { bad.push('place.' + k + ' must be a number'); return; }
      n = Math.min(PLACE_NUM[k][1], Math.max(PLACE_NUM[k][0], n));
      out[k] = (k === 'w' || k === 'h' || k === 'z') ? pyRound(n, 0) : pyRound(n, 4);
    });
    if ('by' in p) { if (has(PLACED_BY, p.by)) out.by = p.by; else bad.push('place.by must be one of ' + PLACED_BY.join('|')); }
    if ('layer' in p) { if (has(LAYERS, p.layer)) out.layer = p.layer; else bad.push('place.layer must be one of ' + LAYERS.join('|')); }
    if ('locked' in p) out.locked = !!p.locked;
    return { place: Object.keys(out).length ? out : null, bad: bad };
  }

  function normGroup(g) {
    if (g === null || g === undefined || g === '' || (isObj(g) && !Object.keys(g).length)) return { group: {}, bad: [] };
    if (!isObj(g) || !String(g.id || '').trim()) return { group: null, bad: ['group must be {id, front?} or {} to separate'] };
    return { group: { id: String(g.id).trim().slice(0, 80), front: !!g.front }, bad: [] };
  }

  function normalise(block) {
    var b = isObj(block) ? block : {};
    var place = normPlace(isObj(b.place) ? b.place : null).place || {};
    var group = normGroup(isObj(b.group) ? b.group : null).group || {};
    var size = String(b.size || 'm'), view = String(b.view || '');
    var src = isObj(b.source) ? b.source : null;
    if (!src && b.type === 'result' && isObj(b.content) && b.content.cap) {
      var c = b.content; src = { cap: c.cap, args: c.args || {}, ts: c.ts || '', ms: c.ms || 0 };
    }
    var anchors = Array.isArray(b.anchors) ? b.anchors : (isObj(b.anchor) ? [b.anchor] : []);
    var state = has(STATES, b.state) ? b.state : 'now';
    return {
      key: String(b.key || b.id || ''), id: b.id === undefined ? null : b.id,
      kind: String(b.type || b.kind || 'note'), content: isObj(b.content) ? b.content : {},
      source: src || {}, anchors: anchors, state: state, face: faceOf({ face: b.face, state: state }),
      size: has(SIZES, size) ? size : 'm', place: place, locked: !!place.locked,
      view: has(VIEWS, view) ? view : '', group: group, folded: !!b.folded,
      ts: b.ts || '', score: b.score === undefined ? null : b.score
    };
  }

  function applySet(block, fields) {
    var probs = [], want = {}, f = fields || {};
    Object.keys(f).forEach(function (k) {
      var v = f[k]; if (v === null || v === undefined || !has(SETTABLE, k)) return;
      if (k === 'face') { if (!has(FACES, v)) probs.push('face must be one of ' + FACES.join('|')); else want.face = v; }
      else if (k === 'state') { if (!has(STATES, v)) probs.push('state must be one of ' + STATES.join('|')); else want.state = v; }
      else if (k === 'size') { var s = String(v).toLowerCase(); if (!has(SIZES, s)) probs.push('size must be one of ' + SIZES.join('|')); else want.size = s; }
      else if (k === 'view') { if (!has(VIEWS, v)) probs.push('view must be one of ' + VIEWS.join('|')); else want.view = v; }
      else if (k === 'place') { var r = normPlace(v, isObj(block.place) ? block.place : null); probs = probs.concat(r.bad); if (r.place) want.place = r.place; }
      else if (k === 'locked') { var p = isObj(block.place) ? clone(block.place) : {}; var w = want.place || {}; for (var q in w) p[q] = w[q]; p.locked = !!v; want.place = p; }
      else if (k === 'folded') want.folded = !!v;
      else if (k === 'group') { var g = normGroup(v); probs = probs.concat(g.bad); if (g.group) want.group = g.group; }
    });
    if (probs.length) return { changed: {}, prev: {}, problems: probs };
    if (has(['tile', 'window', 'full'], want.face) && !('state' in want) && block.state === 'parked') want.state = 'now';
    var changed = {}, prev = {};
    Object.keys(want).forEach(function (k) {
      var old = k === 'face' ? faceOf(block) : block[k];
      if (!eq(old, want[k])) { prev[k] = old === undefined ? null : old; changed[k] = want[k]; block[k] = want[k]; }
    });
    return { changed: changed, prev: prev, problems: [] };
  }

  /* Decision 4: bodies declare, the container draws. */
  var BODIES = {};
  var DEFAULT_BODY = { views: [], fit: 'content', minSize: 's', actions: [] };
  function registerBody(kind, spec) {
    kind = String(kind || ''); if (!kind) return null; spec = isObj(spec) ? spec : {};
    var b = { kind: kind, draw: typeof spec.draw === 'function' ? spec.draw : null,
              views: Array.isArray(spec.views) ? spec.views.filter(function (v) { return has(VIEWS, v); }) : [],
              fit: has(['content', 'fill', 'fixed'], spec.fit) ? spec.fit : 'content',
              minSize: has(SIZES, spec.minSize) ? spec.minSize : 's',
              natural: typeof spec.natural === 'function' ? spec.natural : null,
              actions: Array.isArray(spec.actions) ? spec.actions.filter(function (a) { return a && a.id; }).map(function (a) { return clone(a); }) : [] };
    BODIES[kind] = b; return b;
  }
  function body(kind) { return BODIES[kind] || clone(DEFAULT_BODY); }
  function bodies() { return Object.keys(BODIES).sort(); }

  var ROW = ['view', 'lock', 'pill', 'max', 'more', 'drop'];
  var ROW_NARROW = ['lock', 'pill', 'more', 'drop'];
  function controls(kind, o) {
    o = o || {}; var b = body(kind);
    return (o.narrow ? ROW_NARROW : ROW).filter(function (c) { return c !== 'view' || b.views.length > 1; });
  }

  var MENU = ['park', 'size', 'arrange', 'fold', 'group', 'explode', 'edit', 'context', 'refresh', 'dive', 'open', 'copy'];
  function menu(kind, item) {
    var b = body(kind), it = item || {};
    var std = MENU.filter(function (m) { return m !== 'refresh' || (isObj(it.source) && !!it.source.cap); });
    return std.concat(b.actions.map(function (a) { return a.id; }).filter(function (id) { return !has(std, id); }));
  }

  var api = { STATES: STATES, FACES: FACES, SIZES: SIZES, LAYERS: LAYERS, VIEWS: VIEWS, SETTABLE: SETTABLE, Z: Z, z: z,
              ROW: ROW, ROW_NARROW: ROW_NARROW, MENU: MENU, faceOf: faceOf, normalise: normalise, applySet: applySet,
              registerBody: registerBody, body: body, bodies: bodies, controls: controls, menu: menu };
  root.VeraItem = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
