/* VeraItem - the item contract (DEEP-29 section 11A.4), pure: the same in the browser (window.VeraItem) and in Node.

   An item is one thing on a surface (desk/canvas, dashboard grid, LHM slot, chat reply, notebook), drawn as ONE container
   (head + controls + resize + faces) around ONE body. This file is the browser half of the record and the body registry:

     normalise(block)              the item record of a stored block - the same answers as vera/canvas/item_core.py
     applySet(block, fields)       {changed, prev, problems} for a canvas.item.set call, applied in place when valid
     faceOf(block)                 pill | tile | window | full (a block with no face has the one its state implies)
     transition(item, event)       the face state after an event (pill, unpill, open, max, restore, send_back, drag, ...)
     registerBody(kind, spec)      a body declares {draw, views, fit, minSize, actions}; the container draws the head
     body(kind)                    the declared spec, or the default one (one view, fit 'content', minSize 's')
     controls(kind, o)             the head's standard row for a kind, in the ADR's order: view, (dock), lock, pill, max, more,
                                   drop; o.narrow (<320 px) keeps lock, pill, more, drop. A host says what applies (WSU-58 S6):
                                   o.views (the item's own views - more than one draws view), o.window (a kept window: dock),
                                   o.lock / o.pill / o.max / o.more false (left out), o.fold (the column's fold for pill)
     menu(kind, item, o)           the more-menu: the standard entries, then the body's own actions. With o, the host's whole
                                   menu in its one order (MENU_ALL), each entry there when o[entry] is true
     action(kind, id)              a body's registered action (its static label, title, lead, replaces, kit), or null
     Z                             the z-order bands (vera-item.css carries the same table as custom properties)
     z(band, n)                    the z-index for a band; tiles and windows take an offset n inside their range

   Bodies never draw a head. A kind that is not registered still gets the full container. */
(function (root) {
  'use strict';
  /* loaded once per page: a host script that carries this file too (the canvas element, the dashboard - their routes put it
     in front of them) finds the first copy and keeps its registered bodies */
  if (root.VeraItem && typeof root.VeraItem.registerBody === 'function') {
    if (typeof module !== 'undefined' && module.exports) module.exports = root.VeraItem;
    return;
  }

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
            layerFront: 155, flash: 180, drag: 200, pop: 230, menu: 240, chat: 250, sheet: 300, toast: 400 };

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
  /* the head's one row (WSU-58 S6: the canvas head and the dashboard tile draw theirs from this). dock (a window kept as a
     window goes back to the desk) sits after view; fold is the column's pill (it folds the item to its head line). */
  function controls(kind, o) {
    o = o || {}; var b = body(kind); var views = Array.isArray(o.views) ? o.views : b.views; var row = [];
    if (views.length > 1) row.push('view');
    if (o.window) row.push('dock');
    if (o.lock !== false) row.push('lock');
    if (o.pill !== false) row.push(o.fold ? 'fold' : 'pill');
    if (o.max !== false) row.push('max');
    if (o.more !== false) row.push('more');
    row.push('drop');
    return o.narrow ? row.filter(function (c) { return has(ROW_NARROW, c) || c === 'fold'; }) : row;
  }

  var MENU = ['park', 'size', 'arrange', 'fold', 'group', 'explode', 'edit', 'context', 'refresh', 'dive', 'open', 'copy'];
  /* the host's whole menu, in its one order: first what a narrow or peeking head could not show (view, lock, max), then the
     ADR's entries as the canvas has them - park, pin, size, move up / down (a keyless block), back to the arrangement, open in
     place (the column), fold, group, explode, edit, in context, refresh, take it off (a peeking pill), full size behind / in
     front, the deep dives - then open and copy where a host has them; the body's own actions follow */
  var MENU_ALL = ['view', 'lock', 'max', 'park', 'pin', 'size', 'up', 'down', 'arrange', 'inplace', 'fold', 'group', 'explode',
                  'edit', 'context', 'refresh', 'drop', 'layer', 'dive', 'open', 'copy'];
  function menu(kind, item, o) {
    var b = body(kind), it = item || {}; var src = isObj(it.source) && !!it.source.cap;
    var std = o ? MENU_ALL.filter(function (m) { return !!o[m] && (m !== 'refresh' || src); })
                : MENU.filter(function (m) { return m !== 'refresh' || src; });
    return std.concat(b.actions.map(function (a) { return a.id; }).filter(function (id) { return !has(std, id); }));
  }
  function action(kind, id) {
    var acts = body(kind).actions; for (var i = 0; i < acts.length; i++) if (acts[i].id === id) return clone(acts[i]);
    return null;
  }

  /* HOW AN ITEM CHANGES FACE (Decision 3; WSU-58 S2) = item_core.transition, held to the same answers by the shared cases
     (tests/fixtures/item_face_cases.json). faceState(item) -> {face, state, locked, back, dock, pilled}; transition(item,
     event) -> the face state after the event (pure). back: the face a full item returns to; dock: what a window gives when
     sent back ('tile' window mode, 'pill' a pill opened as a window); pilled: the PERSON made it a pill. Owner O2: a
     window stays until it is sent back - blur and focus change nothing. */
  var FACE_EVENTS = ['pill', 'unpill', 'open', 'max', 'restore', 'send_back', 'drag', 'resize_end',
                     'lock', 'unlock', 'park', 'unpark', 'pin', 'unpin', 'focus', 'blur'];
  var LAYOUT_EVENTS = ['no_room', 'to_border', 'from_border'];
  var EVENTS = FACE_EVENTS.concat(LAYOUT_EVENTS);
  function faceState(item) {
    var it = isObj(item) ? item : {};
    var state = has(STATES, it.state) ? it.state : 'now', face = faceOf(it);
    if (state === 'parked') face = 'pill';
    var place = isObj(it.place) ? it.place : {};
    var locked = !!(it.locked || place.locked);
    var back = has(['pill', 'tile', 'window'], it.back) ? it.back : '', dock = has(['tile', 'pill'], it.dock) ? it.dock : '';
    back = face === 'full' ? (back || 'tile') : '';
    dock = (face === 'window' || (face === 'full' && back === 'window')) ? (dock || 'tile') : '';
    var pilled = ('pilled' in it) ? !!it.pilled : ((face === 'pill' || (face === 'full' && back === 'pill')) && state !== 'parked');
    return { face: face, state: state, locked: locked, back: back, dock: dock, pilled: pilled };
  }
  function evOf(ev) {
    if (isObj(ev)) return { type: String(ev.type || ev.event || ''), to: String(ev.to || 'desk'), arrange: String(ev.arrange || '') };
    return { type: String(ev || ''), to: 'desk', arrange: '' };
  }
  function sendBack(s) {
    if (s.face === 'full') { s.face = s.back || 'tile'; s.back = ''; if (s.face !== 'window') s.dock = ''; }
    else if (s.face === 'window') { s.face = s.dock || 'tile'; s.dock = ''; }
  }
  function put(s, o) { for (var k in o) s[k] = o[k]; }
  function transition(item, event) {
    var s = faceState(item), e = evOf(event), t = e.type;
    if (s.state === 'parked' || s.state === 'hidden') {
      if (t === 'unpark' || (t === 'pin' && s.state === 'parked')) put(s, { state: t === 'pin' ? 'pinned' : 'now', face: 'tile', back: '', dock: '', pilled: false });
      return s;
    }
    if (t === 'lock') s.locked = true;
    else if (t === 'unlock') s.locked = false;
    else if (t === 'pin') s.state = 'pinned';
    else if (t === 'unpin') { if (s.state === 'pinned') s.state = 'now'; }
    else if (t === 'park') put(s, { state: 'parked', face: 'pill', back: '', dock: '', pilled: false });
    else if (t === 'unpark') { /* already out */ }
    else if (t === 'max') { if (s.face === 'full') sendBack(s); else { s.back = s.face; s.face = 'full'; } }
    else if (t === 'restore' || t === 'send_back') { if (s.face === 'window' || (t === 'restore' && s.face === 'full')) sendBack(s); }
    else if (t === 'focus' || t === 'blur') { /* O2: a window stays until it is sent back */ }
    else if (s.locked) { /* locked: no pill / unpill / open / drag / resize, nothing from the layout */ }
    else if (t === 'pill') {
      if (s.face === 'full' && s.back === 'window' && s.dock === 'pill') put(s, { back: 'pill', dock: '' });
      else if (s.face === 'full') put(s, { back: 'pill', dock: '', pilled: true });
      else if (s.face === 'window' && s.dock === 'pill') put(s, { face: 'pill', dock: '' });
      else put(s, { face: 'pill', dock: '', pilled: true });
    }
    else if (t === 'unpill') {
      if (s.face === 'pill') put(s, { face: 'tile', pilled: false });
      else if (s.face === 'full' && s.back === 'pill') put(s, { back: 'tile', pilled: false });
    }
    else if (t === 'open') {
      if (s.face === 'pill') { if (s.pilled || e.arrange === 'slots') put(s, { face: 'tile', pilled: false }); else put(s, { face: 'window', dock: 'pill' }); }
    }
    else if (t === 'drag') {
      if (s.face === 'full') sendBack(s);
      if (e.to === 'pills') put(s, { face: 'pill', dock: '' });
      else if (s.face === 'pill') put(s, { face: 'tile', pilled: false });
      else if (s.face === 'window' && s.dock === 'pill') put(s, { face: 'tile', dock: '' });
    }
    else if (t === 'resize_end') { if (s.face === 'window' && s.dock === 'tile') put(s, { face: 'tile', dock: '' }); }
    else if (t === 'no_room') { if (s.face === 'tile') put(s, { face: 'window', dock: 'tile' }); }
    else if (t === 'to_border') { if (s.face === 'tile') put(s, { face: 'pill', pilled: false }); }
    else if (t === 'from_border') { if (s.face === 'pill' && !s.pilled) s.face = 'tile'; }
    return s;
  }

  var api = { STATES: STATES, FACES: FACES, SIZES: SIZES, LAYERS: LAYERS, VIEWS: VIEWS, SETTABLE: SETTABLE, Z: Z, z: z,
            ROW: ROW, ROW_NARROW: ROW_NARROW, MENU: MENU, MENU_ALL: MENU_ALL, faceOf: faceOf, normalise: normalise, applySet: applySet,
            registerBody: registerBody, body: body, bodies: bodies, controls: controls, menu: menu, action: action,
            EVENTS: EVENTS, FACE_EVENTS: FACE_EVENTS, LAYOUT_EVENTS: LAYOUT_EVENTS, faceState: faceState, transition: transition };
  root.VeraItem = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
