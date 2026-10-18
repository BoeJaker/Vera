/* VeraItem - the item contract (DEEP-29 section 11A.4), pure: the same in the browser (window.VeraItem) and in Node.

   An item is one thing on a surface (desk/canvas, dashboard grid, LHM slot, chat reply, notebook), drawn as ONE container
   (head + controls + resize + faces) around ONE body. This file is the browser half of the record and the body registry:

     normalise(block)              the item record of a stored block - the same answers as vera/canvas/item_core.py
     applySet(block, fields)       {changed, prev, problems} for a canvas.item.set call, applied in place when valid
     faceOf(block)                 pill | tile | window | full (a block with no face has the one its state implies)
     transition(item, event)       the face state after an event (pill, unpill, open, max, restore, send_back, drag, ...)
     registerBody(kind, spec)      a body declares {draw, views, fit, minSize, actions}; the container draws the head. It is
                                   also THE element registry (DO-0.2): the same call carries what the thing draws and where
                                   it goes - see ELEMENT REGISTRY below; there is no other element table
     body(kind)                    the declared spec, or the default one (one view, fit 'content', minSize 's')
     element(kind)                 the whole registered spec (a copy, with the dashboard span of each size), or null
     elements(filter)              the registered specs that match {contract, surface ('panel' = any panel:<id>), role, face, element}
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
  /* what a full item returns to, what a window gives when sent back (WSU-58 S7: on the record, = item_core.BACKS / DOCKS) */
  var BACKS = ['pill', 'tile', 'window'], DOCKS = ['tile', 'pill'];
  var SETTABLE = ['face', 'place', 'view', 'size', 'locked', 'folded', 'group', 'state', 'back', 'dock'];
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
    if (block && block.state === 'parked') return 'pill';   // parking wins over a stored face (WSU-58 S2)
    var f = String((block && block.face) || '');
    if (has(FACES, f)) return f;
    return 'tile';
  }
  /* the face a move to `state` gives (= item_core.face_for_state): park -> pill; off the shelf to now / pinned -> tile */
  function faceForState(block, state) {
    var prev = has(STATES, block && block.state) ? block.state : 'now';
    if (state === 'parked' && prev !== 'parked') return 'pill';
    if ((prev === 'parked' || prev === 'hidden') && (state === 'now' || state === 'pinned')) return 'tile';
    return null;
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

  /* the item's composite (WSU-58 S7, = item_core.norm_group): {} the surface's own grouping; id the item the person joined
     it to (LV-11); sep the person took it out; at when its tab was last brought to the front (ms); front it was, as written */
  var GROUP_BAD = "group must be {id?, front?, at?, sep?} naming an id, sep or at - or {} for the surface's own grouping";
  function normGroup(g) {
    if (g === null || g === undefined || g === '' || (isObj(g) && !Object.keys(g).length)) return { group: {}, bad: [] };
    if (!isObj(g)) return { group: null, bad: [GROUP_BAD] };
    var gid = String(g.id || '').trim().slice(0, 80), sep = !!g.sep, at = null;
    if (g.at !== null && g.at !== undefined && g.at !== '') { at = num(g.at); if (at === null) return { group: null, bad: ['group.at must be a number (ms)'] }; }
    if (!gid && !sep && at === null) return { group: null, bad: [GROUP_BAD] };
    var out = {}; if (gid && !sep) out.id = gid; out.front = !!g.front;
    if (at !== null) out.at = pyRound(Math.max(0, at), 0);
    if (sep) out.sep = true;
    return { group: out, bad: [] };
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
    var state = has(STATES, b.state) ? b.state : 'now', face = faceOf({ face: b.face, state: state });
    var back = face === 'full' && has(BACKS, b.back) ? b.back : '';
    var dock = (face === 'window' || (face === 'full' && back === 'window')) && has(DOCKS, b.dock) ? b.dock : '';
    return {
      key: String(b.key || b.id || ''), id: b.id === undefined ? null : b.id,
      kind: String(b.type || b.kind || 'note'), content: isObj(b.content) ? b.content : {},
      source: src || {}, anchors: anchors, state: state, face: face, back: back, dock: dock,
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
      else if (k === 'back' || k === 'dock') { var ok = k === 'back' ? BACKS : DOCKS;   /* '' clears it */
        if (v !== '' && !has(ok, v)) probs.push(k + ' must be one of ' + ok.join('|') + " or ''"); else want[k] = v; }
    });
    if (probs.length) return { changed: {}, prev: {}, problems: probs };
    if (has(['tile', 'window', 'full'], want.face) && !('state' in want) && block.state === 'parked') want.state = 'now';
    if ('state' in want && !('face' in want)) { var ff = faceForState(block, want.state); if (ff) want.face = ff; }
    var changed = {}, prev = {}, face0 = faceOf(block);
    Object.keys(want).forEach(function (k) {
      var old = k === 'face' ? face0 : block[k];
      if (!eq(old, want[k])) { prev[k] = old === undefined ? null : old; changed[k] = want[k]; block[k] = want[k]; }
    });
    return { changed: changed, prev: prev, problems: [] };
  }

  /* Decision 4: bodies declare, the container draws. */
  var BODIES = {};
  var DEFAULT_BODY = { views: [], fit: 'content', minSize: 's', actions: [] };

  /* THE ELEMENT REGISTRY (DO-0.2, DESK-OUTPUT-REDESIGN-PLAN §2.1; backbone review 2026-10-18 points 1-3). One thing, one
     element, registered ONCE - here, by registerBody, with more fields; there is no second element table. What the head and
     the ⋯ menu read (views, fit, minSize, actions -> controls / menu / action) is unchanged; the fields below are for the
     coordinator and the later waves (the resolver, the dashboard, a panel's slot) and change nothing a person sees.
       role        'body' an item kind the container hosts (the canvas's BLOCK kinds) | 'element' a shared element drawn
                   inside bodies and panels (<vera-mkt-chart>, the output kit, ...)
       element     the custom element (or the JS object) that draws it;  src  the file it lives in;  label  its name in words
       contract    [{kind, by, reads, how}] what it draws: kind a presentation kind (by 'present' - present() routes it here),
                   an item kind written directly (by 'item' - canvas.add / the chat), what a host hands it (by 'api' -
                   attributes or methods), or a kind the plan names that nothing routes yet (by 'planned'); reads the keys
       faces       the subset of pill / tile / window / full it is drawn in (the ADR's faces; a panel is a SURFACE, not a face)
       sizes       the subset of the size classes xs-xl; the dashboard maps each class to its span (SPANS - vera-dashboard.js
                   spanFor's own table), so a spec declares classes, never spans; element() adds spans
       controls    [{id, label, title, when}] its OWN controls, drawn in its body (a chart's bar, a table's bar); a display
                   card's actions are in the head / ⋯ menu (actions above), never here
       navigation  [{to, args, how}] where it goes deeper, as intents: to item (a new item of args.kind beside it), panel
                   (args.panel), element (the same element re-pointed - another symbol), drawer (the estate drawer by a
                   <kind>:<id> ref), url / tab (a link), chat; args values in <angle brackets> are read from the data
       search      {cap, how} the capability it searches with, or null
       live        {cap | sub | route, every_s, how} what keeps it current (a cap it re-reads, a subscription or socket, a
                   route it polls), or null
       surfaces    where it is drawn today: desk, dashboard, chat, lhm, panel:<id>
       note        anything a reader of the registry should know (what is not built yet) */
  var SURFACES = ['desk', 'dashboard', 'chat', 'lhm'];   /* and panel:<id> */
  var SPANS = { xs: [2, 1], s: [2, 1], m: [4, 2], l: [6, 2], xl: [8, 4] };
  var ROLES = ['body', 'element'];
  var CONTRACT_BY = ['present', 'item', 'api', 'planned'];
  var NAV_TO = ['item', 'panel', 'element', 'drawer', 'url', 'tab', 'chat'];
  function str(v) { return typeof v === 'string' ? v : ''; }
  function strs(a) { var out = []; (Array.isArray(a) ? a : []).forEach(function (x) { if (typeof x === 'string' && x && !has(out, x)) out.push(x); }); return out; }
  function data(v) { return v === undefined ? undefined : JSON.parse(JSON.stringify(v)); }
  function surfaceOk(s) { return has(SURFACES, s) || /^panel:[a-z0-9_-]+$/.test(s); }
  function normContract(a) {
    return (Array.isArray(a) ? a : []).filter(function (c) { return isObj(c) && str(c.kind) && has(CONTRACT_BY, c.by); })
      .map(function (c) { return { kind: c.kind, by: c.by, reads: strs(c.reads), how: str(c.how) }; });
  }
  function normNav(a) {
    return (Array.isArray(a) ? a : []).filter(function (v) { return isObj(v) && has(NAV_TO, v.to); })
      .map(function (v) { return { to: v.to, args: isObj(v.args) ? data(v.args) : {}, how: str(v.how) }; });
  }
  function normControls(a) {
    return (Array.isArray(a) ? a : []).filter(function (c) { return isObj(c) && str(c.id); })
      .map(function (c) { var o = { id: c.id, label: str(c.label) || c.id }; if (str(c.title)) o.title = c.title; if (str(c.when)) o.when = c.when; return o; });
  }
  function normSearch(s) { return isObj(s) && str(s.cap) ? { cap: s.cap, how: str(s.how) } : null; }
  function normLive(l) {
    if (!isObj(l) || !(str(l.cap) || str(l.sub) || str(l.route))) return null;
    var o = {}; ['cap', 'sub', 'route', 'how'].forEach(function (k) { if (str(l[k])) o[k] = l[k]; });
    if (typeof l.every_s === 'number' || str(l.every_s)) o.every_s = l.every_s;
    return o;
  }
  function spansOf(sizes) { var o = {}; sizes.forEach(function (s) { o[s] = SPANS[s].slice(); }); return o; }

  function registerBody(kind, spec) {
    kind = String(kind || ''); if (!kind) return null; spec = isObj(spec) ? spec : {};
    var b = { kind: kind, draw: typeof spec.draw === 'function' ? spec.draw : null,
              views: Array.isArray(spec.views) ? spec.views.filter(function (v) { return has(VIEWS, v); }) : [],
              fit: has(['content', 'fill', 'fixed'], spec.fit) ? spec.fit : 'content',
              minSize: has(SIZES, spec.minSize) ? spec.minSize : 's',
              natural: typeof spec.natural === 'function' ? spec.natural : null,
              actions: Array.isArray(spec.actions) ? spec.actions.filter(function (a) { return a && a.id; }).map(function (a) { return clone(a); }) : [] };
    /* the registry's fields (DO-0.2) */
    b.role = has(ROLES, spec.role) ? spec.role : 'body';
    b.element = str(spec.element); b.src = str(spec.src); b.label = str(spec.label); b.note = str(spec.note);
    b.contract = normContract(spec.contract);
    var fl = Array.isArray(spec.faces) ? spec.faces : (b.role === 'body' ? FACES : []);
    b.faces = FACES.filter(function (f) { return has(fl, f); });
    var ss = Array.isArray(spec.sizes) ? spec.sizes : (b.role === 'body' ? SIZES.slice(SIZES.indexOf(b.minSize)) : []);
    b.sizes = SIZES.filter(function (s) { return has(ss, s); });
    b.spans = spansOf(b.sizes);
    b.controls = normControls(spec.controls);
    b.navigation = normNav(spec.navigation);
    b.search = normSearch(spec.search);
    b.live = normLive(spec.live);
    b.surfaces = strs(spec.surfaces).filter(surfaceOk);
    BODIES[kind] = b; return b;
  }
  function body(kind) { return BODIES[kind] || clone(DEFAULT_BODY); }
  function bodies() { return Object.keys(BODIES).sort(); }
  /* the whole spec, as a copy (a reader can change it without changing the registry); the functions (draw, natural) kept */
  function element(kind) {
    var b = BODIES[kind]; if (!b) return null;
    var o = {}; Object.keys(b).forEach(function (k) { o[k] = typeof b[k] === 'function' || b[k] === null ? b[k] : data(b[k]); });
    return o;
  }
  function elements(filter) {
    var f = isObj(filter) ? filter : {};
    return bodies().map(element).filter(function (e) {
      if (f.contract && !e.contract.some(function (c) { return c.kind === f.contract; })) return false;
      if (f.surface && !e.surfaces.some(function (s) { return s === f.surface || (f.surface === 'panel' && s.indexOf('panel:') === 0); })) return false;
      if (f.role && e.role !== f.role) return false;
      if (f.face && !has(e.faces, f.face)) return false;
      if (f.element && e.element !== f.element) return false;
      return true;
    });
  }

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
    var back = has(BACKS, it.back) ? it.back : '', dock = has(DOCKS, it.dock) ? it.dock : '';
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

  /* THE RICH SHARED ELEMENTS THAT EXIST TODAY (DO-0.2; plan §1.3 and §0A: the panel-derived full elements come first). Each
     is registered here, once, pointing at its element - as it behaves today (read-only: none of them changed). The canvas's
     body kinds register from their own file (canvas_element.js, BODY_SPEC). A surface is listed where the element is drawn
     now, by its tag or its object. */
  var VIEWED = ['tile', 'window', 'full'];   /* an element inside an item's body: a pill shows the head only */
  registerBody('vera-mkt-chart', { role: 'element', element: 'vera-mkt-chart', src: 'vera/markets/markets_chart.js', label: 'Markets chart',
    contract: [{ kind: 'series', by: 'present', reads: ['symbol', 'provider', 'timeframe'], how: "a markets.chart answer: the widget record (form candles) whose data names its symbol - the canvas's widget body draws it with this element (WSU-75)" },
      { kind: 'ohlcv', by: 'api', reads: ['symbol', 'provider', 'tf'], how: 'attributes; bars from /markets/bars, indicators from /markets/indicators (computed on the server), the market\'s markers from markets.alerts.list, markets.annotate.list, markets.monitor.status, markets.portfolio.tx_list; setSignals(entries, exits)' },
      { kind: 'ohlcv', by: 'planned', reads: [], how: 'the ohlcv presentation kind (plan §2.5): a declaration routes to this element, the candles form an alias of it' }],
    faces: VIEWED, sizes: ['m', 'l', 'xl'],
    controls: [{ id: 'tf', label: 'Timeframes', title: 'the timeframes the asset has bars for; each re-reads its bars' },
      { id: 'ind', label: 'Indicators', title: 'EMA 20, SMA 50, Bollinger, VWAP on the price; RSI, MACD in their own time-synced panes (from 420 px)' },
      { id: 'vol', label: 'Vol', title: 'volume under the candles' }, { id: 'fit', label: 'Fit', title: 'fit the whole history' },
      { id: 'zoom', label: 'Zoom and pan', title: 'wheel zoom, drag pan' }, { id: 'crosshair', label: 'Read-out', title: 'O H L C V of the bar under the pointer' },
      { id: 'search', label: 'Search assets', title: 'the shared <vera-mkt-lookup> in its header: a pick switches the chart' }, { id: 'back', label: 'Back', title: 'the asset before' }],
    navigation: [{ to: 'element', args: { element: 'vera-mkt-chart', symbol: '<symbol>', provider: '<provider>', tf: '<tf>' }, how: 'another symbol: the search, Back, or the symbol attribute (observed); the host hears mkt-symbol' },
      { to: 'view', args: { view: 'news' }, how: "the asset's headlines (markets.news.feed with its symbol_key; searched when opened, at most every 10 min) - the Quant Studio's rows" },
      { to: 'view', args: { view: 'pos' }, how: "the real book's holding in the asset (markets.portfolio.positions, its pulse stats) and its recorded trades - the Quant Studio's holdings table" },
      { to: 'view', args: { view: 'alerts' }, how: "the monitors on the asset and their signals (markets.monitor.status, markets.alerts.list) - the Quant Studio's alert feed" },
      { to: 'view', args: { view: 'bt' }, how: "backtests run on the asset (markets.backtest.list); one picked puts its trades on the candles on its timeframe - the Markets page's rows" }],
    search: { cap: 'markets.lookup', how: 'in the chart\'s own header: <vera-mkt-lookup>, the Markets page\'s and the Quant Studio\'s search (one implementation)' },
    live: { route: '/markets/bars', every_s: 15, how: 'the newest bars while it is seen (IntersectionObserver, page visible); the markers read once a minute per asset and timeframe' },
    surfaces: ['desk', 'panel:markets'],
    note: 'panel:markets uses its shared pieces (VeraMktChart.*: the loader, chart options, series adapters, panes, indicators, markers), not the tag. Not a dashboard tile yet (plan §1.5).' });
  registerBody('vera-mkt-map', { role: 'element', element: 'vera-mkt-map', src: 'vera/markets/markets_chart.js', label: 'Market map',
    contract: [{ kind: 'parts', by: 'present', reads: ['groups'], how: "a markets.overview answer: its treemap record holding the groups of assets - the canvas's widget body draws it with this element" },
      { kind: 'parts', by: 'api', reads: ['window'], how: "/markets/overview's groups: the Quant Studio's Pulse map - each asset a box sized and coloured by its move over the window (VeraMktChart.drawMarketMap, the Studio draws with it)" }],
    faces: VIEWED, sizes: ['m', 'l', 'xl'],
    controls: [{ id: 'win', label: 'Window', title: "1D, 1W, 1M, 3M, YTD, 1Y - the Studio's Pulse windows" }],
    navigation: [{ to: 'element', args: { element: 'vera-mkt-chart', symbol: '<symbol>', provider: '<provider>', tf: '1d' }, how: "an asset's box opens its chart in place; ← Map returns; the host hears mkt-symbol" }],
    live: { route: '/markets/overview', every_s: 60, how: 'read again every minute while it is seen and the page visible (the server caches it 60 s)' },
    surfaces: ['desk', 'panel:markets-studio'],
    note: 'panel:markets-studio draws its Pulse map with the shared drawMarketMap, not the tag. markets.overview declares the treemap form; elsewhere (the dashboard) that form draws the groups by size.' });
  registerBody('vera-mkt-folio', { role: 'element', element: 'vera-mkt-folio', src: 'vera/markets/markets_chart.js', label: 'Portfolio',
    contract: [{ kind: 'table', by: 'present', reads: ['positions', 'totals'], how: "a markets.portfolio.positions answer: its spark-table record holding positions + totals - the canvas's widget body draws it with this element" },
      { kind: 'table', by: 'api', reads: [], how: "/markets/portfolio/positions: the Quant Studio's Portfolio (the real book) - its tiles and holdings table with the pulse stats (VeraMktChart.realBook / tilesHtml / rows.holdings / fillHoldings, the Studio draws with them)" }],
    faces: VIEWED, sizes: ['m', 'l', 'xl'],
    controls: [],
    navigation: [{ to: 'element', args: { element: 'vera-mkt-chart', symbol: '<symbol>', provider: '<provider>', tf: '1d' }, how: "a holding's row opens its chart in place; ← Portfolio returns; the host hears mkt-symbol" }],
    live: { route: '/markets/portfolio/positions', every_s: 60, how: 'read again every minute while it is seen and the page visible' },
    surfaces: ['desk', 'panel:markets-studio'],
    note: 'Read-only: the ledger (the real bookkeeping) stays in the Quant Studio. panel:markets-studio draws its Portfolio with the shared pieces, not the tag.' });
  registerBody('vera-graph', { role: 'element', element: 'veraUI.Graph', src: 'vera/vera_graph.js', label: 'Graph',
    contract: [{ kind: 'graph', by: 'api', reads: ['layer', 'layerOpts', 'nodes', 'edges'], how: 'veraUI.Graph: a snapshot by layer (memory, entity, a fabric.graphs graph) through fetchSnapshot, or the nodes and edges a host loads' }],
    faces: [], sizes: [],
    controls: [{ id: 'search', label: 'Search' }, { id: 'legend', label: 'Legend' }, { id: 'rail', label: 'Left rail' }, { id: 'layers', label: 'Layer switcher' },
      { id: 'modes', label: 'Display modes', title: 'Graph, Exploded, Estate 3D / 2D, Mermaid (vera_graph_modes.js)' }, { id: 'node-actions', label: 'Node actions', when: 'actionsEnabled' }],
    navigation: [{ to: 'drawer', args: { node: '<node.id>' }, how: "a node's detail drawer; its actions from /fabric/graph/node_actions, run through /fabric/graph/run_node_action" }],
    live: { sub: 'the node action\'s progress event on the shared WebSocket', how: 'new entities, records and edges are added as they appear; the snapshot is read again when the action ends' },
    surfaces: ['chat', 'panel:fabric-panel', 'panel:dream-panel', 'panel:notebook-panel'],
    note: 'a JS object (/ui/vera-graph.js), not a custom element; the desk draws graphs through vera-graph-embed, its bare configuration. In the chat page it is the memory graph.' });
  registerBody('vera-graph-embed', { role: 'element', element: 'vera-graph-embed', src: 'vera/graph_embed_element.js', label: 'Graph (bare)',
    contract: [{ kind: 'graph', by: 'api', reads: ['layer', 'params', 'limit', 'height'], how: 'a fabric graph snapshot by layer, no chrome - vera-graph configured bare, not a fork' },
      { kind: 'code', by: 'api', reads: ['renderer', 'code', 'lang', 'path', 'text', 'record', 'ranges', 'mode', 'layers', 'depth'], how: 'renderer="struct": the Explode contract drawn by <vera-structgraph>' }],
    faces: VIEWED, sizes: ['m', 'l', 'xl'],
    controls: [{ id: 'zoom', label: 'Pan and zoom' }, { id: 'resize', label: 'Resize grip', when: 'resize is not "off"' }, { id: 'expand', label: 'Open in the full view', when: 'expand is not "off"' }],
    navigation: [{ to: 'panel', args: { panel: 'fabric-panel', hash: 'graph' }, how: 'the expand affordance opens full-url (default /fabric/panel#graph)' },
      { to: 'item', args: { kind: 'code', line: '<node.props.start_line>' }, how: 'vera-graph-node: the host (the canvas) lights and scrolls the bound source to the node\'s lines' }],
    surfaces: ['desk', 'chat'],
    note: "desk: the explode item and the graph view of table / kv / entities / records; chat: the explode diagram; the output kit's code explode; <vera-widget>'s structured-graph slots." });
  registerBody('vera-out', { role: 'element', element: 'VeraOut', src: 'vera/vera_output_element.js', label: 'Output kit (code, prose, diagram, file cards)',
    contract: [{ kind: 'code', by: 'api', reads: ['lang', 'code', 'filename'], how: 'VeraOut.card: the numbered code card an explode binds to' },
      { kind: 'document', by: 'api', reads: ['md'], how: 'VeraOut.render: prose with its fences as cards; proseMenu under it' },
      { kind: 'diagram', by: 'api', reads: ['mermaid', 'title'], how: 'VeraOut.diagram: the diagram card over <vera-mermaid>' },
      { kind: 'file', by: 'api', reads: ['lang', 'code', 'title'], how: 'VeraOut.fileCard: a deck, a sheet (csv, tsv) or a document drawn as what it is' }],
    faces: VIEWED, sizes: ['s', 'm', 'l', 'xl'],
    controls: [{ id: 'copy', label: 'Copy' }, { id: 'run', label: 'Run' }, { id: 'edit', label: 'Edit' }, { id: 'preview', label: 'Preview' }, { id: 'dsrc', label: 'Source' },
      { id: 'present', label: 'Present' }, { id: 'outline', label: 'Outline' }, { id: 'fpop', label: 'Pop out (file)' }, { id: 'pop', label: 'Pop out' }, { id: 'dpop', label: 'Pop out (diagram)' },
      { id: 'save', label: 'Save' }, { id: 'dsvg', label: 'SVG' }, { id: 'dpng', label: 'PNG' }, { id: 'send', label: 'Send to', title: 'an HTML report, a podcast, a notebook, a file, a project, a channel' }],
    navigation: [{ to: 'item', args: { kind: 'explode', code: '<code>' }, how: 'Explode: the structured diagram of the code, beside it' }],
    surfaces: ['chat', 'desk', 'panel:agent-loop-output', 'panel:dag-workshop'],
    note: "on the desk the bar is hidden (bar: false) and its actions are the item's head actions (the canvas's code body registers them, vo:*)." });
  registerBody('vera-mermaid', { role: 'element', element: 'vera-mermaid', src: 'vera/render/vera_mermaid.js', label: 'Mermaid diagram',
    contract: [{ kind: 'diagram', by: 'api', reads: ['code', 'title', 'max-height', 'bare', 'fill', 'theme-variables'], how: 'flowchart, sequence, state and pie, drawn without a CDN; stream(code) while it arrives' }],
    faces: VIEWED, sizes: ['s', 'm', 'l', 'xl'],
    controls: [{ id: 'zoom', label: 'Pan and zoom' }, { id: 'fit', label: 'Fit' }, { id: 'svg', label: 'Export SVG' }, { id: 'png', label: 'Export PNG' }, { id: 'popout', label: 'Pop out', when: 'not bare' }],
    navigation: [{ to: 'chat', args: { mermaid: '<code>' }, how: "vm:popout - the chat's own diagram pop-out" }],
    surfaces: ['chat', 'desk', 'panel:notebook-panel', 'panel:vera-mermaid'],
    note: "its toolbar is hidden when bare (the desk's diagram item, a notebook cell); <vera-widget>'s pipes form and the exploded scene draw with it too." });
  registerBody('vera-terminal', { role: 'element', element: 'vera-terminal', src: 'vera/remote/vera-terminal.js', label: 'Terminal',
    contract: [{ kind: 'terminal', by: 'api', reads: ['ws'], how: "xterm over Vera's terminal WebSocket (ssh host or docker container); a single-use ticket from POST /remote/term/ticket first" }],
    faces: VIEWED, sizes: ['m', 'l', 'xl'],
    controls: [],
    live: { sub: 'the terminal WebSocket (the ws attribute)', how: 'output as it comes; keys and resizes back' },
    surfaces: ['desk', 'lhm', 'panel:workspaces', 'panel:workers-ollama', 'panel:models'],
    note: "desk: the session item; lhm: the sandbox quick terminal; the Remote panel's page draws it too." });
  registerBody('vera-flow-builder', { role: 'element', element: 'vera-flow-builder', src: 'vera/elements/flow_builder_element.js', label: 'Flow builder',
    contract: [{ kind: 'flow', by: 'api', reads: ['provider', 'source'], how: "setProvider(domain adapter) then loadFromSource(doc); palette -> SVG DAG -> inspector; serialize() back to the host's document" }],
    faces: [], sizes: [],
    controls: [{ id: 'palette', label: 'Palette search' }, { id: 'auto', label: 'Auto layout' }, { id: 'clear', label: 'Clear' }, { id: 'state', label: 'State' }, { id: 'compat', label: 'Compatibility' },
      { id: 'ir', label: 'Workflow IR', title: 'export, import, preview, apply' }, { id: 'node', label: 'Node edit / delete' }],
    surfaces: ['chat', 'panel:flow-builder', 'panel:research-panel', 'panel:markets', 'panel:dream-panel', 'panel:fabric-panel'],
    note: "not an item on the desk today; chat: the DAG and dream builders of the command view." });
  registerBody('vera-research-card', { role: 'element', element: 'vera-research-card', src: 'vera/research_card_element.js', label: 'Research card',
    contract: [{ kind: 'research', by: 'api', reads: ['jobId', 'push(message)', 'finish(report)'], how: "the researcher's own messages (token, step, citations, crawl_*, review, iter_*, error, done): status, steps, method, pages read, sources, report - updated in place" }],
    faces: [], sizes: [],
    controls: [],
    navigation: [{ to: 'url', args: { url: '<source.url>' }, how: 'vera:research:source - a page it read or cites' }],
    live: { sub: "the researcher's WebSocket (/ws/stream/<job>), pushed by the host", how: 'research:done when it ends' },
    surfaces: ['chat', 'panel:agent-loop-output', 'panel:dag-workshop'],
    note: "the desk's research item is the canvas's own research body, not this element (plan §1.3: shared in Wave 4)." });
  registerBody('vera-exploded', { role: 'element', element: 'vera-exploded', src: 'vera/chat/exploded_element.js', label: 'Exploded scene',
    contract: [{ kind: 'scene', by: 'api', reads: ['turns', 'sel', 'mode'], how: 'setScene({turns, sel}): a station per turn - read, exchange, produced, landed - in cards, front and iso' },
      { kind: 'code', by: 'api', reads: ['layers', 'links'], how: 'contractScene(doc): the code and prose explodes (code.explode, nlp.explode.prose) as a scene' }],
    faces: [], sizes: [],
    controls: [{ id: 'mode', label: 'Cards / Front / Iso' }, { id: 'zoom', label: 'Zoom' }, { id: 'tilt', label: 'Tilt and swing' }, { id: 'layers', label: 'Layers', title: 'activity, estate, galaxy' },
      { id: 'solo', label: 'Solo' }, { id: 'stack', label: 'Stack' }],
    navigation: [{ to: 'chat', args: { mid: '<turn.mid>' }, how: 'vera:xpl:pick / vera:xpl:turn - the turn in the transcript' }],
    surfaces: ['chat'],
    note: "the chat's Explode; vera-graph's Exploded mode draws it too. The desk's explode item is the canvas's own body (plan §1.3: shared in Wave 4)." });

  /* the shared kit (looplab-control §9 Phase K, vera-44; agreed with the desk 19 Oct - backbone §12): THE timeline, matrix,
     drill panel / live strip / states, compare workspace and diff viewer. Each draws records it is handed or reads its own
     caps through /mcp/call; Loop Lab's sections are built from them, and the desk's Wave 5 timelines land on vera-timeline. */
  registerBody('vera-timeline', { role: 'element', element: 'vera-timeline', src: 'vera/ui/vera_timeline.js', label: 'Timeline (lanes)',
    contract: [{ kind: 'lanes', by: 'api', reads: ['lanes', 'from', 'to'], how: 'lane RECORDS ({lane, label, group, type band|bar|mark|strip|tiles|chip|graph, source {cap, args}, map, live}) a family registers once; the element reads each lane\'s source, one read per shared source' },
      { kind: 'lanes', by: 'api', reads: ['draw.lanes', 'draw.lane_specs', 'draw.from', 'draw.to'], how: "the dashboard's 'lanes' widget form (VeraWidget R.lanes): lane ids, lane records it carries, and the range" }],
    faces: VIEWED, sizes: ['m', 'l', 'xl'],
    controls: [],
    navigation: [{ to: 'element', args: { element: 'vera-drill', kind: '<lane drill kind>', id: '<item id>' }, how: 'timeline:select {lane, item} - the host opens the item in its drill; timeline:brush {from, to} zooms' }],
    search: null,
    live: { route: '/mcp/call', every_s: 60, how: "each lane's own live seconds; a failed read (an unknown capability) is shown on the lane as an error, never as an empty lane" },
    surfaces: ['dashboard', 'panel:evolve'],
    note: 'No controls on its face (range, lane chips and drill belong to the host). model.overlay draws guide lines across every lane (Compare\'s pins). THE timeline: other timelines migrate onto it (desk Wave 5).' });
  registerBody('vera-matrix', { role: 'element', element: 'vera-matrix', src: 'vera/ui/vera_matrix.js', label: 'Matrix (tiles)',
    contract: [{ kind: 'matrix', by: 'api', reads: ['map.cells', 'map.row', 'map.col', 'map.value', 'colour', 'ok', 'group', 'sort', 'window'], how: 'a matrix RECORD over a cap answer (source) or handed cells (data): rows x columns, a value map or a numeric scale, row % and column totals when ok says what green is' }],
    faces: VIEWED, sizes: ['m', 'l', 'xl'],
    controls: [],
    navigation: [{ to: 'element', args: { element: 'vera-drill', kind: '<host kind>', id: '<cell>' }, how: 'matrix:select {row, col, cell} - the host opens the cell' }],
    search: null,
    live: { route: '/mcp/call', every_s: 0, how: 'read once from its source, or redrawn when the host sets a new record' },
    surfaces: ['panel:evolve'],
    note: 'Loop Lab: tests race to green, candidate matrix (applications x candidates), release stages. Grouping, sorting and the window are the record\'s, set by the host.' });
  registerBody('vera-drill', { role: 'element', element: 'vera-drill', src: 'vera/ui/vera_kit.js', label: 'Drill panel',
    contract: [{ kind: 'drill', by: 'api', reads: ['kind', 'id', 'tab'], how: 'a path of frames; a KIND is a record a family registers (load, title, tabs, render); <a data-drill="kind:id"> inside a body opens a deeper frame' }],
    faces: VIEWED, sizes: ['m', 'l', 'xl'],
    controls: [{ id: 'crumbs', label: 'Breadcrumbs' }, { id: 'back', label: 'Back' }, { id: 'tabs', label: 'Tabs' }, { id: 'close', label: 'Close' }],
    navigation: [{ to: 'element', args: { element: 'vera-drill', kind: '<kind>', id: '<id>' }, how: 'a deeper frame on the same path; the path rides in the hash (drill=kind:id@tab/...), so a link or a reload reopens it' }],
    search: null,
    live: null,
    surfaces: ['panel:evolve'],
    note: 'The same file has <vera-live-strip> (what is running now, from live records) and VeraStates (loading, empty, error, stale, broken - one look). Its own controls are the drill\'s chrome: it IS the control area.' });
  registerBody('vera-compare', { role: 'element', element: 'vera-compare', src: 'vera/ui/vera_compare.js', label: 'Compare',
    contract: [{ kind: 'pins', by: 'api', reads: ['pins'], how: 'pins are records (a lane, or an item with its time, carrying its lane record) kept per viewer; drawn with vera-timeline and vera-drill' }],
    faces: ['window', 'full'], sizes: ['l', 'xl'],
    controls: [{ id: 'mode', label: 'Stacked / layered / side by side' }, { id: 'window', label: '± window' }, { id: 'anchor', label: 'Align on a pin' }, { id: 'unpin', label: 'Unpin' }, { id: 'clear', label: 'Clear pins' }],
    navigation: [{ to: 'element', args: { element: 'vera-drill', kind: '<pin drill kind>', id: '<pin id>' }, how: 'side by side opens each pinned item in its drill' }],
    search: null,
    live: { route: '/mcp/call', every_s: 0, how: 'each pinned lane is re-read from its own source when the pins change' },
    surfaces: ['panel:evolve'],
    note: 'Aligned on an item: equal windows before / after, cut to now, with the nearest items as evidence; a host can align it (setAnchor).' });
  registerBody('vera-diff', { role: 'element', element: 'vera-diff', src: 'vera/ui/vera_diff.js', label: 'Diff viewer',
    contract: [{ kind: 'diff', by: 'api', reads: ['pipeline'], how: 'evolve.pipeline.diff + evolve.pipeline.lint for a pipeline id, or a unified diff and findings a host hands it (setDiff)' }],
    faces: VIEWED, sizes: ['m', 'l', 'xl'],
    controls: [{ id: 'view', label: 'Unified / side by side', title: 'u' }, { id: 'fold', label: 'Unfold', title: 'e, or click a fold' }, { id: 'keys', label: 'Hunk and file keys', title: 'j / k hunk, n / p file' }],
    navigation: [{ to: 'element', args: { element: 'vera-diff', path: '<path>', line: '<line>' }, how: 'diff:select {path, line} when a line number is clicked' }],
    search: null,
    live: null,
    surfaces: ['panel:evolve'],
    note: 'Word-level marks, syntax by file type (offline tokenizer), file tree with +/- and lint counts; lint shown on its line, a checker that could not run listed as absent. vera/ide/ide_changes_panel.html can adopt it.' });

  var api = { STATES: STATES, FACES: FACES, SIZES: SIZES, LAYERS: LAYERS, VIEWS: VIEWS, BACKS: BACKS, DOCKS: DOCKS, SETTABLE: SETTABLE, Z: Z, z: z,
            ROW: ROW, ROW_NARROW: ROW_NARROW, MENU: MENU, MENU_ALL: MENU_ALL, faceOf: faceOf, normalise: normalise, applySet: applySet,
            registerBody: registerBody, body: body, bodies: bodies, controls: controls, menu: menu, action: action,
            element: element, elements: elements, SPANS: SPANS, SURFACES: SURFACES, ROLES: ROLES, CONTRACT_BY: CONTRACT_BY, NAV_TO: NAV_TO,
            EVENTS: EVENTS, FACE_EVENTS: FACE_EVENTS, LAYOUT_EVENTS: LAYOUT_EVENTS, faceState: faceState, transition: transition };
  root.VeraItem = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
