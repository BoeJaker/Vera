/* VeraLattice - the iso lattice: planes (named, ordered layers) of nodes standing in areas (named columns), the pipes between
   them on channel tracks, iso widgets beside every node, and a map of a view (pan, zoom, fit, a rotation about the planes'
   centre). Estate > Live ops draws with it; so do vera-graph's "Live ops" method and anything that mounts it.

   VeraLattice.engine(env)  -> the drawing engine on a page that owns its own chrome (Live ops): env = { S, $?, byId?, PCOL, LCOL,
                               KCOL, SC, wspec(n), lensCol?, lensDim?, lensTag?, pill?, extraParts?(ID, pos, NSC, W, H), afterDraw? }
                               S is the page's state (view, layout3d, focus, pipes, widgets, lbl, vz/vx/vy, tilt/azim, zgap,
                               spread, wsz, on, snap = {planes, domains, nodes, links, inflight?, series?}, ...).
   VeraLattice.mount(host, o) -> a self-contained view: o = { planes:[{id, name, col?}], areas:[name], nodes:[{id, label, plane,
                               area (index or name), status?, kind?, load?, ...}], links:[{a, b, kind?}], view?, layout?, widgets?,
                               labels?, onPick?(node), onOpen?(node) } -> { set(data), option(k, v), fit(), render(), destroy() }
   VeraLattice.CSS            the plot's stylesheet (Live ops keeps its own copy in step).
   The code below the engine line is Live ops' own, moved here unchanged but for its hooks. */
(function(){
'use strict';
if (window.VeraLattice) return;
function engine(env){
const S = env.S;
const $ = env.$ || ((id) => document.getElementById(id));
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const px = (v) => v.toFixed(1) + 'px';
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const PCOL = env.PCOL, LCOL = env.LCOL, KCOL = env.KCOL, SC = env.SC;
const byId = env.byId || (() => { const m = {}; ((S.snap && S.snap.nodes) || []).forEach((n) => { m[n.id] = n; }); return m; });
const wspec = env.wspec || ((n) => wdefaults(n));
const lensCol = env.lensCol || (() => null), lensDim = env.lensDim || (() => false), lensTag = env.lensTag || (() => null);
// ── the engine (Live ops' own code) ──
const ROWS = 5, SLOTS = { 1: [2], 2: [1, 3], 3: [1, 2, 3], 4: [0, 1, 3, 4], 5: [0, 1, 2, 3, 4] };
const slotsFor = (n) => SLOTS[n] || Array.from({ length: n }, (_, i) => (ROWS - 1) * i / Math.max(n - 1, 1));
const tempCol = (t) => t >= 70 ? 'var(--ac3)' : t >= 60 ? 'var(--dv4)' : 'var(--ac2)';
const tc = (v) => v >= 70 ? 'var(--ac4)' : v >= 55 ? 'var(--ac3)' : 'var(--ac2)';
// the widget kinds: [kind, name, what it reads]
const WKINDS = [['server', 'server', 'a machine: drives lit by its load, a lamp for its state'], ['dial', 'dial', 'load'], ['tank', 'tank', 'disk'], ['thermo', 'thermometer', 'temperature'], ['tower', 'tower', 'load as height'],
  ['crates', 'crates', 'containers'], ['chip', 'chip', 'studs lit by work'], ['mast', 'mast', 'signal'], ['conveyor', 'conveyor', 'work in flight'], ['stack', 'stack', 'a pipeline'], ['term', 'terminal', 'a shell on the machine: its last lines; click to type'], ['client', 'client', 'a client of Vera: a screen, lit by who it is'],
  ['mem', 'memory', 'memory in use, stick by stick'], ['bars', 'history', 'the last readings as columns'], ['hist', 'latency', 'how long its requests took'],
  ['shield', 'registration', 'SSH login, directory, mesh door, certificate, backup'], ['gpu', 'GPU card', 'a GPU, its fans lit while it works'], ['models', 'models', 'the models it holds'],
  ['db', 'database', 'a store, lit while it works'], ['queue', 'queue', 'work in flight through it'], ['nic', 'network', 'its pipes or connections as port lights']];
const WNAME = {}; WKINDS.forEach((k) => { WNAME[k[0]] = k[1]; });
// the widgets of a node stand in one line beside its pin; each takes this much of the lane (ground units), its size included
const wstep = (w) => (w.k === 'term' ? 6.0 : 2.4) * S.wsz * (w.s || 1);
// how much of the screen the chrome may take as the view zooms: chips, labels and cards hold between these
const UI_MIN = 0.8, UI_MAX = 1.35;
const uiScale = (vz) => (vz >= 0.2 ? clamp(vz, UI_MIN, UI_MAX) : UI_MIN * Math.max(0.5, vz / 0.2)) / vz;   // labels keep their size down to 20%, then shrink gently
const REGP = ['ssh', 'directory', 'mesh', 'certificate', 'backup'];
const regYes = (st) => /^(yes|ok|done|enrolled|present|on)$/.test(String(st || '').toLowerCase());
let DEG = null, DEG_OF = null;
const degOf = (id) => { if (DEG_OF !== S.snap){ DEG_OF = S.snap; DEG = {}; ((S.snap && S.snap.links) || []).forEach((l) => { DEG[l.a] = (DEG[l.a] || 0) + 1; DEG[l.b] = (DEG[l.b] || 0) + 1; }); } return DEG[id] || 0; };
const regOf = (n) => { const r = n.reg; if (!r || !r.planes) return null; const ks = Object.keys(r.planes).filter((k) => r.planes[k].state !== 'n/a'); return { yes: ks.filter((k) => regYes(r.planes[k].state)).length, of: ks.length, complete: !!r.complete, missing: ks.filter((k) => !regYes(r.planes[k].state)) }; };
// what a node wears by default: what it is, then every reading the estate has for it (the rail overrides the list)
function wdefaults(n){ const L = []; const st = (k, s) => L.push({ k, s: s || 1 });
  const ser = (S.snap && S.snap.series && S.snap.series[n.id]) || {}, hist = !!((ser.cpu && ser.cpu.length > 1) || (ser.tps && ser.tps.length > 1) || (ser.ping && ser.ping.length > 1));
  if (n.kind === 'guest' || n.kind === 'host' || n.kind === 'docker-host'){ st('server'); if (n.load != null) st('dial', .8); if (n.mem_pct != null) st('mem', .8); if (n.disk_pct != null) st('tank', .8);
    if (n.temp != null) st('thermo', .8); if (hist) st('bars', .8); if (n.reg) st('shield', .8); if (n.kind === 'docker-host') st('crates', .8); }
  else if (n.kind === 'docker'){ st('server'); st('crates', .8); st('nic', .8); }
  else if (n.kind === 'ollama'){ st(/gpu/i.test(n.label + ' ' + n.detail) ? 'gpu' : 'chip'); st('dial', .8); if ((n.caps || []).length) st('models', .8); if (n.latency && n.latency.length) st('hist', .8); if (hist) st('bars', .8); }
  else if (n.kind === 'store'){ st('db'); st('queue', .8); }
  else if (n.kind === 'core'){ st('chip'); st('queue', .8); if (hist) st('bars', .8); }
  else if (n.kind === 'sandbox'){ st('server'); st('crates', .8); }
  else if (n.kind === 'mesh'){ st('mast'); if (hist) st('bars', .8); }
  else if (n.id.indexOf('pipe:') === 0){ st('stack'); }
  else if (n.kind === 'work'){ st('conveyor'); st('tower', .8); }
  else if (n.kind === 'client'){ st('client'); st('nic', .8); }
  else st('chip');
  return L; }
function wcaption(n, k){ if (k === 'term'){ const T = S.term[n.id]; return T && T.lines.length ? 'shell · ' + T.lines.length + ' lines' : 'shell'; }
  if (k === 'mem') return 'mem ' + Math.round(n.mem_pct || 0) + '%';
  if (k === 'bars'){ const s = (S.snap.series || {})[n.id] || {}, last = (a) => (a && a.length ? a[a.length - 1] : null);
    return s.tps ? (last(s.tps) != null ? last(s.tps).toFixed(1) : '—') + ' tok/s' : s.cpu ? 'cpu ' + Math.round(last(s.cpu) || 0) + '%' : s.ping ? 'ping ' + Math.round(last(s.ping) || 0) + ' ms' : 'history'; }
  if (k === 'hist') return 'p95 ' + (n.p95_ms != null ? Math.round(n.p95_ms) + 'ms' : '—');
  if (k === 'shield'){ const r = regOf(n); return r ? 'reg ' + r.yes + '/' + r.of : 'no registration'; }
  if (k === 'gpu') return n.inflight ? n.inflight + ' working' : 'idle';
  if (k === 'models') return (n.caps || []).length + ' models';
  if (k === 'db') return n.status + (n.inflight ? ' · ' + n.inflight + ' busy' : '');
  if (k === 'queue') return (n.inflight || 0) + ' in flight';
  if (k === 'nic') return n.conns ? n.conns + ' connections' : degOf(n.id) + ' pipes';
  if (k === 'client') return String(n.detail || n.ckind || 'client').split(' · ').slice(0, 2).join(' · ');
  if (k === 'server') return (n.status === 'down' ? 'DOWN · ' : n.errors ? n.errors + (n.errors === 1 ? ' finding · ' : ' findings · ') : '') + (n.kind === 'docker' ? 'docker · ' + String(n.detail).replace(' running', '') : (n.type === 'qemu' ? 'VM' : n.kind === 'guest' ? 'CT' : n.kind === 'docker-host' ? 'docker host' : 'host') + ' · ' + (n.load != null ? 'load ' + Math.round(n.load) + '%' : n.status));
  if (k === 'dial') return 'load ' + Math.round(n.load || 0) + '%'; if (k === 'tank') return (n.disk_pct != null ? 'disk ' + Math.round(n.disk_pct) : 'load ' + Math.round(n.load || 0)) + '%';
  if (k === 'thermo') return Math.round(n.temp || 0) + '° · ' + ((n.temp || 0) >= 70 ? 'hot' : (n.temp || 0) >= 55 ? 'warm' : 'cool'); if (k === 'tower') return (n.load != null ? 'load ' + Math.round(n.load) + '%' : n.status);
  if (k === 'crates') return n.kind === 'docker' ? String(n.detail).replace(' running', '') : n.kind === 'sandbox' ? (n.detail || 'sandbox').split(' · ')[0] : (n.inflight || 0) + ' in flight';
  if (k === 'chip') return (n.inflight ? n.inflight + ' in flight' : n.status); if (k === 'mast') return (n.detail || '').split(' · ')[0] || n.status; if (k === 'conveyor') return (n.inflight || 0) + ' in flight'; if (k === 'stack') return (n.detail || '').split(' · ')[0] || n.status; return n.status; }

// ── layout: the lattice (2D bands, or iso floors carrying the same lattice, each as deep as its fullest cell) ──
function layout(W, H){
  const snap = S.snap, ISO = window.VeraISO;
  const PL = snap.planes.filter((p) => S.on[p.id]);
  const rowOf = {}; PL.forEach((p, i) => { rowOf[p.id] = i; });
  const shown = snap.nodes.filter((n) => S.on[n.plane]);
  // a plane's label says how many stand on it, how many are down and how many findings they carry
  const psum = (pid) => { const ns = shown.filter((n) => n.plane === pid), dn = ns.filter((n) => n.status === 'down').length, fd = ns.reduce((t, n) => t + (n.errors || 0), 0);
    return ' · ' + ns.length + (dn ? ' · ' + dn + ' down' : '') + (fd ? ' · ' + fd + (fd === 1 ? ' finding' : ' findings') : ''); };
  const slot = {}; shown.forEach((n) => { const k = n.plane + ':' + n.domain; slot[k] = (slot[k] || 0) + 1; });
  const cellMax = {}; Object.keys(slot).forEach((k) => { const pl = k.split(':')[0]; cellMax[pl] = Math.max(cellMax[pl] || 0, slot[k]); });
  const used = {};
  const AXIS = 30, rows = PL.length || 1, SP = S.spread;
  const pos = {}, bands = [], floors = [], planeLabels = [], domLabels = [], L = {};
  let route = null, G = null;
  const busPts = [];
  const DOM = snap.domains;
  const LX = 104, RX = 12, colW = (W - LX - RX) / DOM.length, colX = (d) => LX + (d + 0.5) * colW;
  const ladder = (pl) => clamp(Math.ceil((cellMax[pl] || 1)), S.view === '2d' ? 2 : 5, S.view === '2d' ? 12 : 9);   // 2D: a band as tall as its fullest cell
  const slotOf = (total, idx, R) => total <= 5 ? slotsFor(total)[idx] * (R - 1) / (ROWS - 1) : (R - 1) * idx / Math.max(total - 1, 1);
  const stagOf = (total, idx) => total > 8 ? ((idx % 3) - 1) : total > 5 ? ((idx % 2) ? 1 : -1) : 0;
  if (S.view === '2d' || !ISO){
    // a band is as tall as its ladder asks (a row every 30 px × spread), never less than a chip's worth; the stage scrolls
    const G2 = 110;   // each column keeps its left 110 px for pipes: eight tracks 12 px apart
    const bandNeed = (pl) => 60 + (ladder(pl) - 1) * 88 * SP, needSum = PL.reduce((t, p) => t + bandNeed(p.id), 0) || 1;
    const scale = Math.max(1, (H - AXIS) / needSum);
    const bandHOf = {}, bandY = {}; let yy = 0; PL.forEach((p) => { bandY[p.id] = yy; bandHOf[p.id] = bandNeed(p.id) * scale; yy += bandHOf[p.id]; });
    PL.forEach((p) => { const y = bandY[p.id]; bands.push({ y, h: bandHOf[p.id], col: PCOL[p.id] }); planeLabels.push({ x: 10, y: y + 8, col: PCOL[p.id], t: p.name + psum(p.id) }); });
    DOM.forEach((d, i) => domLabels.push({ x: colX(i), y: yy + 9, t: d }));
    shown.forEach((n) => {
      const k = n.plane + ':' + n.domain; const idx = (used[k] = (used[k] || 0)); const total = slot[k]; used[k]++;
      const bh = bandHOf[n.plane], R = ladder(n.plane), rowP = (bh - 40) / (R - 1);
      const y = bandY[n.plane] + bh / 2 + 6 + (slotOf(total, idx, R) - (R - 1) / 2) * rowP;
      // chips stand in the column's zone right of its gutter (G2 px, the pipes' own)
      const zL = LX + n.domain * colW + G2, zW = colW - G2;
      const hwc = Math.min(zW / 2 - 4, (60 + 5.6 * Math.min(26, n.label.length) + (n.errors ? 20 : 0) + (n.inflight ? 12 : 0) + (n.load != null || n.mem_pct != null || n.disk_pct != null ? 16 : 0)) / 2 + 6);
      pos[n.id] = { x: clamp(zL + zW / 2 + stagOf(total, idx) * zW * (total > 8 ? .24 : .18), zL + hwc, zL + zW - hwc), y, col: PCOL[n.plane], d: n.domain, rp: rowP };
    });
    // 2D channel routing: a pipe drops from its chip into the gap beside its row (the side of its target), runs along the gap to the
    // gutter at the left of the target's column, along the gutter to the gap beside the target's row, and into the chip. It never
    // crosses a chip. Tracks are dealt per channel by interval, as in 3D: overlapping runs never share one.
    let TR2 = null; const T2 = 7, TG = 12;   // row-gap tracks 7 px apart, gutter tracks 12
    const laneOf2 = {}, laneN2 = {}; const lane2 = (grp, key) => { const k = grp + '|' + key; if (laneOf2[k] == null){ const n = (laneN2[grp] = (laneN2[grp] || 0) + 1) - 1; laneOf2[k] = n; } return laneOf2[k]; };
    const sd = (A, B) => (B.y > A.y + 1 ? 1 : -1);
    const chA = (A, B) => A.y + sd(A, B) * A.rp / 2, chB = (A, B) => (Math.abs(B.y - A.y) <= 1 ? chA(A, B) : B.y - sd(A, B) * B.rp / 2);
    const gxOf = (B) => LX + B.d * colW + 6;
    const build2 = () => { TR2 = {}; const ch = {}; const use = (c, key, part, lo, hi) => { (ch[c] = ch[c] || []).push({ key, part, lo: Math.min(lo, hi), hi: Math.max(lo, hi) }); };
      (S.snap.links || []).forEach((l) => { const A = pos[l.a], B = pos[l.b]; if (!A || !B) return; const key = l.a + '>' + l.b; if (TR2[key]) return; TR2[key] = {};
        const ya = chA(A, B), yb = chB(A, B), g = gxOf(B); use('h:' + Math.round(ya), key, 'ha', A.x, g); use('v:' + B.d, key, 'g', ya, yb); use('h:' + Math.round(yb), key, 'hb', g, B.x); });
      Object.keys(ch).forEach((c) => { const ends = [];
        ch[c].sort((x, y) => x.lo - y.lo || y.hi - x.hi).forEach((r) => { let k = ends.findIndex((e) => e < r.lo - 6); if (k < 0){ k = ends.length; ends.push(r.hi); } else ends[k] = r.hi; TR2[r.key][r.part] = k; }); }); };
    route = (a, b) => { const A = pos[a], B = pos[b], key = a + '>' + b; if (!TR2) build2(); const T = TR2[key] || {}, tk = (x) => (T[x] || 0) % 8;
      const ya = chA(A, B) + (tk('ha') - 3.5) * T2, yb = chB(A, B) + (tk('hb') - 3.5) * T2, gx = gxOf(B) + tk('g') * TG;
      // each pipe has its own port along the chip, 8 px apart: a busy node's pipes fan out rather than share one line
      const xa = A.x + (lane2('pa:' + a, key) % 9 - 4) * 11, xb = B.x + (lane2('pb:' + b, key) % 9 - 4) * 11;
      return [{ x: xa, y: A.y }, { x: xa, y: ya }, { x: gx, y: ya }, { x: gx, y: yb }, { x: xb, y: yb }, { x: xb, y: B.y }]; };
  } else {
    const TILT = S.tilt == null ? (S.layout3d === 'flat' ? 34 : 26) : S.tilt, AZIM = clamp(S.azim == null ? 45 : S.azim, 26, 64);
    const TH = TILT * Math.PI / 180, FLAT = S.layout3d === 'flat';
    // the room a node takes: its pin and chip, and the widgets that stand beside it (up to three wide)
    // the widgets stand in a line along u, each as long as its size asks: the fullest line of a plane sets its lanes and rows
    // the room a node takes: its widget line (u) - each cell as wide as ITS nodes' lines; rows as deep as the widgets are big
    const wline = {}, maxL = {}, maxS = {}; shown.forEach((n) => { const ws = S.widgets ? wspec(n) : []; wline[n.id] = ws.reduce((t, w) => t + wstep(w), 0);
      const ck = n.plane + ':' + n.domain; maxL[ck] = Math.max(maxL[ck] || 0, wline[n.id]); maxS[n.plane] = Math.max(maxS[n.plane] || 0, ws.reduce((m, w) => Math.max(m, w.s || 1), 0)); });
    const cellW = (pl, d) => 3.4 * SP + (maxL[pl + ':' + d] || 0);   /* gutter + pin 2.4, the widget line, 1.0 to spare */   // the pin's gutter (1.4 wide) holds eight pipe lanes                              // u between two nodes of a cell
    const rowV = (pl) => 1.2 * SP + 1.2 * S.wsz * (maxS[pl] || 0) + 1.6 * SP;   // the widgets' depth for their size + the row gap's pipe tracks   // + the row gap, eight pipe tracks   // v between two rows of a cell
    // a crowded cell is a grid, not one ladder: six rows at most, then another column
    const ROWCAP = 6, grid = {};
    Object.keys(slot).forEach((k) => { const t = slot[k], cols = Math.max(1, Math.ceil(t / ROWCAP)); grid[k] = { cols, rows: Math.ceil(t / cols) }; });
    // each plane sizes its OWN domain columns (as wide as its cell there, a sliver where it has none) and is centred in the stack:
    // aligning every plane's columns to the busiest plane left the sparse planes mostly empty floor
    const domUp = {}, domLp = {}, UWp = {}; PL.forEach((p) => { let u = 0; domLp[p.id] = []; domUp[p.id] = DOM.map((d, i) => { const g = grid[p.id + ':' + i]; const w = g ? g.cols * cellW(p.id, i) + 0.5 * SP : 0.6 * SP; domLp[p.id][i] = u + 0.25 * SP; const c = u + w / 2; u += w; return c; }); UWp[p.id] = u; });
    const UW = Math.max.apply(null, [1].concat(PL.map((p) => UWp[p.id])));
    const rowsOf = (pl) => Math.max.apply(null, [1].concat(Object.keys(grid).filter((k) => k.split(':')[0] === pl).map((k) => grid[k].rows)));
    const VHof = {}; PL.forEach((p) => { VHof[p.id] = ((rowsOf(p.id) - 1) / 2) * rowV(p.id) + 1.2 * SP + 0.8 * S.wsz * (maxS[p.id] || 0); });   /* front margin for the widgets' depth */
    const VMAX = Math.max.apply(null, PL.map((p) => VHof[p.id]));
    const KI0 = (FLAT ? 0.74 : 1) * 60 / Math.cos(45 * Math.PI / 180), NHW = 52, FITM = 14;
    const uOff = {}, vOff = {}; { const rowsD = [];
      PL.forEach((p, i) => { const r = Math.floor(i / 3); rowsD[r] = Math.max(rowsD[r] || 0, VHof[p.id]); });
      PL.forEach((p, i) => { const c = i % 3, r = Math.floor(i / 3); uOff[p.id] = c * (UW + 4.2 * SP); let v = 0; for (let j = 0; j < r; j++) v += 2 * rowsD[j] + 5.4 * SP; vOff[p.id] = v + (rowsD[r] - VHof[p.id]); }); }
    // where the risers stand: a bus line along the BACK edge of the planes, outside every plate (the front is left to the nodes)
    const vBusOf = (pl) => -((FLAT ? VHof[pl] : VMAX) + 0.45 * SP);
    const lay = (KI) => {
      const uyStable = KI * Math.SQRT1_2 * Math.sin(TH);
      const spanY = 2 * VMAX * uyStable + 4 * uyStable;
      // the floors stand a floor's own depth apart on screen whatever the tilt (z is divided by cos(tilt)): tilting never squashes the stack
      const zh = Math.max(40, spanY + 26) * S.zgap / Math.max(0.2, Math.cos(TH));
      const iso = ISO.proj(TILT, AZIM, KI, true);
      const zOf = (pl) => FLAT ? 0 : (rows - 1 - rowOf[pl]) * zh, uOf = (pl) => FLAT ? uOff[pl] : (UW - UWp[pl]) / 2, vOf2 = (pl) => FLAT ? vOff[pl] : 0;
      const rawX = [], rawY = [], corners = [], rawNodes = [], LL = {}, usedL = {}, pcs = [];
      PL.forEach((p) => { const z = zOf(p.id), uo = uOf(p.id), vo = vOf2(p.id), VH = VHof[p.id], vb = vBusOf(p.id) - 3.2 * SP;
        // the plate holds its routes: the gutters left of the first column, the bus lanes along the back
        const uL = -0.8 * SP, vBk = vBusOf(p.id) - 3.3 * SP;
        const UP = UWp[p.id] + 0.8 * SP, c = [iso(uL + uo, vBk + vo, z), iso(UP + uo, vBk + vo, z), iso(UP + uo, VH + vo, z), iso(uL + uo, VH + vo, z)];
        corners.push({ p, c }); pcs.push([(UP + uL) / 2 + uo, (vBk + VH) / 2 + vo, z]); c.forEach((q) => { rawX.push(q[0]); rawY.push(q[1]); }); });
      shown.forEach((n) => { const k = n.plane + ':' + n.domain; const idx = (usedL[k] = (usedL[k] || 0)); usedL[k]++; const g = grid[k];
        const c = idx % g.cols, r = Math.floor(idx / g.cols);
        const u = domLp[n.plane][n.domain] + c * cellW(n.plane, n.domain) + 2.4 * SP + uOf(n.plane),   /* the pin just right of its gutter; its widgets fill the cell */ vv = (r - (g.rows - 1) / 2) * rowV(n.plane) + vOf2(n.plane), z = zOf(n.plane);
        const q = iso(u, vv, z); LL[n.id] = { u, v: vv, z, pl: n.plane, vb: vBusOf(n.plane) + vOf2(n.plane), rv: rowV(n.plane) };
        rawNodes.push({ id: n.id, x: q[0], y: q[1], col: PCOL[n.plane] }); rawX.push(q[0] - NHW, q[0] + NHW); rawY.push(q[1] - 90, q[1] + 16); });
      const minX = Math.min.apply(null, rawX), maxX = Math.max.apply(null, rawX), minY = Math.min.apply(null, rawY), maxY = Math.max.apply(null, rawY);
      const pn = pcs.length || 1, pc = pcs.reduce((a, q) => [a[0] + q[0], a[1] + q[1], a[2] + q[2]], [0, 0, 0]);
      return { iso, corners, rawNodes, LL, minX, maxX, minY, maxY, pv: iso(pc[0] / pn, pc[1] / pn, pc[2] / pn) };
    };
    // fit the width always (a wide lattice would be cut at the plot's edge); fit the height too when the planes lie flat -
    // a stack asks the plot for its height instead (render grows the plot and the stage scrolls)
    let KI = KI0, R = lay(KI);
    for (let i = 0; i < 5; i++){ const s = Math.min(1, (W - 2 * FITM) / (R.maxX - R.minX), FLAT ? (H - AXIS - 2 * FITM) / (R.maxY - R.minY) : 1); if (s > 0.995) break; KI = Math.max(KI0 * (FLAT ? 0.45 : !S.widgets ? 0.6 : 0.9), KI * s); R = lay(KI); }
    const ox = (W - (R.maxX - R.minX)) / 2 - R.minX, oy = Math.max(FITM - R.minY, (H - AXIS - (R.maxY - R.minY)) / 2 - R.minY);
    G = { pivot: { x: R.pv[0] + ox, y: R.pv[1] + oy }, iso: R.iso, ox, oy, L: R.LL, FLAT, fitS: KI / KI0, TILT, AZIM, SP, height: R.maxY - R.minY + AXIS + 2 * FITM, width: R.maxX - R.minX + 2 * FITM };
    R.corners.forEach((f) => { floors.push({ id: f.p.id, col: PCOL[f.p.id], rail: !!f.rail, pts: f.c.map((q) => (q[0] + ox).toFixed(1) + ',' + (q[1] + oy).toFixed(1)).join(' ') }); if (f.rail) return;
      const a = f.c[3]; planeLabels.push({ id: f.p.id, x: clamp(a[0] + ox - 2, 8, W - 96), y: clamp(a[1] + oy - 15, 10, Math.max(H, G.height) - 14), col: PCOL[f.p.id], t: f.p.name + psum(f.p.id) + (S.focus === f.p.id ? ' · in focus' : '') }); });
    const vAx = VMAX + 1.0 * SP;
    const PLb = PL[PL.length - 1] || {}; DOM.forEach((d, i) => { const q = R.iso((domUp[PLb.id] || [])[i] + (FLAT ? 0 : (UW - (UWp[PLb.id] || UW)) / 2), vAx, 0); domLabels.push({ x: clamp(q[0] + ox, 34, W - 34), y: q[1] + oy, t: d }); });
    R.rawNodes.forEach((n) => { pos[n.id] = { x: n.x + ox, y: n.y + oy, col: n.col }; });
    Object.assign(L, R.LL);
    // pipes: within a plane along its lanes; between planes out to the plane's front bus, along it to a riser that stands
    // outside every plate, straight up or down, and in along the other plane's bus - no riser crosses a node or a stem.
    // Risers from one column fan out beside each other; their feet are the planes' bus points.
    const P = (u, v, z) => { const q = R.iso(u, v, z); return { x: q[0] + ox, y: q[1] + oy }; }; G.P = P;
    // every pipe keeps a lane of its own - within a plane a small step off the row, on the bus a line further back - so no two
    // runs lie on top of each other and a selected route can be followed end to end
    const riserN = {}, riserOf = {}, laneN = {}, laneOf = {};
    const lane = (grp, key) => { const k = grp + '|' + key; if (laneOf[k] == null){ const n = (laneN[grp] = (laneN[grp] || 0) + 1) - 1; laneOf[k] = n % 8; } return laneOf[k]; };
    // a pipe runs in the channels between the nodes: out of its pin to the gutter at the left of its column (the widgets stand
    // to the right), along the gap behind a row, and in to the other pin from ITS gutter - it never crosses a pin, a widget or
    // a stem. Between planes it takes its gutter straight back to the plane's back bus. Every channel gives each pipe a lane.
    // channel routing: every pipe run is a TRACK in a channel - a gutter (left of a column of pins, along v), a row gap (behind a
    // row, along u) or a plane's back bus (along u). Tracks are dealt per channel by interval: two runs that overlap along the
    // channel never share a track; runs that do not, may. Each END of a pipe also keeps a height tier among the pipes sharing it
    // (a floor seen nearly edge-on squashes side-by-side tracks; height does not squash), so pipes into a hub fan out.
    const LN = 0.23 * SP, LB = 0.3 * SP, ZS = 5.5 / Math.max(0.2, Math.cos(TH)), GUT = 1.25 * SP, NT = 12;
    let TR = null;
    const gx = (N) => N.u - GUT, gkey = (N) => 'g:' + N.pl + ':' + Math.round(N.u * 10), rkey = (N) => 'r:' + N.pl + ':' + Math.round(N.v * 10);
    const buildTracks = () => { TR = {}; const ch = {};
      const use = (c, key, part, lo, hi) => { (ch[c] = ch[c] || []).push({ key, part, lo: Math.min(lo, hi), hi: Math.max(lo, hi) }); };
      (S.snap.links || []).forEach((l) => { const A = L[l.a], B = L[l.b]; if (!A || !B) return; const key = l.a + '>' + l.b; if (TR[key]) return; TR[key] = {};
        if (A.pl === B.pl){ if (Math.abs(A.u - B.u) < 1e-6){ use(gkey(A), key, 'gA', A.v, B.v); return; }
          const hv = A.v + (B.v > A.v + 1e-6 ? 1 : -1) * A.rv / 2; use(gkey(A), key, 'gA', A.v, hv); use('r:' + A.pl + ':' + Math.round(hv * 10), key, 'r', gx(A), gx(B)); use(gkey(B), key, 'gB', hv, B.v); return; }
        use(gkey(A), key, 'gA', A.v, A.vb); use(gkey(B), key, 'gB', B.vb, B.v);
        if (FLAT) use('b:' + A.pl, key, 'bA', gx(A), gx(B)); else { use('b:' + A.pl, key, 'bA', gx(A) - 0.6 * SP, gx(A) + 0.6 * SP); use('b:' + B.pl, key, 'bB', gx(A), gx(B)); } });
      Object.keys(ch).forEach((c) => { const ends = [];
        ch[c].sort((x, y) => x.lo - y.lo || y.hi - x.hi).forEach((r) => { let k = ends.findIndex((e) => e < r.lo - 0.15 * SP); if (k < 0){ k = ends.length; ends.push(r.hi); } else ends[k] = r.hi; TR[r.key][r.part] = k; }); }); };
    route = (a, b) => { const A = L[a], B = L[b], key = a + '>' + b; if (!TR) buildTracks(); const T = TR[key] || {};
      const tr = (x) => (T[x] || 0) % NT;
      const zA = (1 + lane('s:' + a, key) % 6) * ZS, zB = (1 + lane('d:' + b, key) % 6) * ZS;   // five low tiers: no tall ticks at the pins
      const gA = gx(A) - (tr('gA') - 5.5) * LN, gB = gx(B) - (tr('gB') - 5.5) * LN;   // a track's step aside and its tier add on screen
      // points as [u, v, plate z, lift]; each keeps its world place (w) so the 3D pipes can be depth-sorted with the widgets
      const at = (pts) => pts.map((q) => { const o = P(q[0], q[1], q[2] + q[3]); o.w = [q[0], q[1], q[2] + q[3], q[2]]; return o; });
      // each end meets its pin at its own port: the short run between the pin and the gutter sits a little aside per pipe
      const pa = (lane('s:' + a, key) % 7 - 3) * 0.07 * SP, pb = (lane('d:' + b, key) % 7 - 3) * 0.07 * SP;
      const port = (pts) => { const n0 = pts.length; pts[1][1] += pa; pts[2][1] += pa; pts[n0 - 3][1] += pb; pts[n0 - 2][1] += pb; return pts; };
      if (A.pl === B.pl){
        if (Math.abs(A.u - B.u) < 1e-6) return at(port([[A.u, A.v, A.z, 0], [A.u, A.v, A.z, zA], [gA, A.v, A.z, zA], [gA, B.v, A.z, zA], [B.u, B.v, B.z, zA], [B.u, B.v, B.z, 0]]));
        const hv = A.v + (B.v > A.v + 1e-6 ? 1 : -1) * A.rv / 2 - (tr('r') - 5.5) * LN;   // the gap on the side of the target: no U-turn
        return at(port([[A.u, A.v, A.z, 0], [A.u, A.v, A.z, zA], [gA, A.v, A.z, zA], [gA, hv, A.z, zA], [gB, hv, A.z, zA], [gB, hv, B.z, zB], [gB, B.v, B.z, zB], [B.u, B.v, B.z, zB], [B.u, B.v, B.z, 0]])); }
      const va = A.vb - tr('bA') * LB, vbB = FLAT ? B.vb : B.vb - tr('bB') * LB;
      const bp = (u, v, zb, lift) => { const o = P(u, v, zb + lift); o.w = [u, v, zb + lift, zb]; return o; };
      if (riserOf[key] == null){ const col = A.pl + ':' + Math.round(A.u * 10); const n = (riserN[col] = (riserN[col] || 0) + 1) - 1; riserOf[key] = gA + (n % 2 ? 1 : -1) * Math.ceil(n / 2) * 0.45 * SP; if (!FLAT){ const lo = (pl) => (UW - UWp[pl]) / 2 - 0.4 * SP, hi = (pl) => (UW - UWp[pl]) / 2 + UWp[pl] + 0.4 * SP; riserOf[key] = clamp(riserOf[key], Math.max(lo(A.pl), lo(B.pl)), Math.min(hi(A.pl), hi(B.pl))); }
        busPts.push(bp(riserOf[key], va, A.z, zA), bp(riserOf[key], FLAT ? vbB : vbB, B.z, zB)); }
      const uR = riserOf[key];
      if (FLAT) return at(port([[A.u, A.v, A.z, 0], [A.u, A.v, A.z, zA], [gA, A.v, A.z, zA], [gA, va, A.z, zA], [gB, va, A.z, zA], [gB, va, B.z, zB], [gB, vbB, B.z, zB], [gB, B.v, B.z, zB], [B.u, B.v, B.z, zB], [B.u, B.v, B.z, 0]]));
      return at(port([[A.u, A.v, A.z, 0], [A.u, A.v, A.z, zA], [gA, A.v, A.z, zA], [gA, va, A.z, zA], [uR, va, A.z, zA], [uR, va, B.z, zB], [uR, vbB, B.z, zB], [gB, vbB, B.z, zB], [gB, B.v, B.z, zB], [B.u, B.v, B.z, zB], [B.u, B.v, B.z, 0]])); };
  }
  return { PL, shown, pos, bands, floors, planeLabels, domLabels, route, G, L, AXIS, cellMax, ladder, busPts };
}

// ── the iso widgets beside their nodes (3D): several per node, built from boxes with the map's own projection ──
function isoWidgets(lay, W, H){
  const ISO = window.VeraISO, out = { faces: [], labels: [], needles: [], edges: [], boxes: [], hits: [], frames: [], groups: [], alarms: [] };
  if (!ISO || !lay.G || !S.widgets) return out;
  const { iso, ox, oy, L, FLAT, TILT, AZIM, SP } = lay.G;
  const Pg = (u, v, z) => { const q = iso(u, v, z); return [q[0] + ox, q[1] + oy]; };
  const ws = (FLAT ? Math.max(.62, Math.min(1, lay.G.fitS * 1.35)) : 1) * S.wsz;
  const sceneG = (g, boxes, key, k) => { const Pd = ISO.proj(TILT, AZIM, k); let u0 = Infinity, v0 = Infinity, u1 = -Infinity, v1 = -Infinity;
    boxes.forEach((b) => { u0 = Math.min(u0, b.u); v0 = Math.min(v0, b.v); u1 = Math.max(u1, b.u + b.w); v1 = Math.max(v1, b.v + b.d); });
    const cu = (u0 + u1) / 2, cv = (v0 + v1) / 2; const f = ISO.scene(Pd, boxes.map((b) => Object.assign({}, b, { u: b.u - cu, v: b.v - cv })), key);
    f.forEach((q) => { q.x += g[0]; q.y += g[1]; }); return { faces: ISO.px(f), at: (u, v, z) => { const p = Pd(u - cu, v - cv, z || 0); return [p[0] + g[0], p[1] + g[1]]; } }; };
  const bboxOf = (faces) => { let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity; faces.forEach((q) => { const x = parseFloat(q.x), y = parseFloat(q.y), w = parseFloat(q.w), h = parseFloat(q.h); x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x + w); y1 = Math.max(y1, y + h); }); return { x0, y0, x1, y1 }; };
  const stackKey = (b) => (b.u + b.v) * 100 + b.z;
  // a terminal: a screen standing on the plate (ISO.frame), its live lines laid onto the screen's face by a css matrix
  const termFrame = (g, k, n) => { const P1 = ISO.proj(TILT, AZIM, 1, true), s = k / 20, FW = 150, FH = 86;
    const f = ISO.frame(P1, { stand: true, W: FW, H: FH, s, col: 'var(--surf3)', col2: 'var(--bd2)', pad: 5, dep: 5, foot: 8 });
    const c = P1(FW * s / 2, 2.5 * s, 0), dx = g[0] - c[0], dy = g[1] - c[1];
    const faces = f.faces.map((q) => Object.assign({}, q, { x: (parseFloat(q.x) + dx).toFixed(1) + 'px', y: (parseFloat(q.y) + dy).toFixed(1) + 'px' }));
    return { faces, col: 'var(--ac2)', frame: { x: parseFloat(f.x) + dx, y: parseFloat(f.y) + dy, w: FW, h: FH, tf: f.tf } }; };
  const BUILD = {
    // a server: a chassis on rails, drive bays along its front lit by its load, a power lamp for its state; a VM is a
    // taller unit with a second layer (the hypervisor's), a docker host wears the whale's blue lid
    server: (g, k, n) => { const vm = n.type === 'qemu', Wd = 3.0, Dd = 1.6, Hh = vm ? 1.15 : 0.78, down = n.status === 'down';
      const lit = down ? 0 : Math.max(1, Math.min(4, Math.ceil(((n.load != null ? n.load : 30) / 100) * 4)));
      const lid = n.kind === 'docker' ? 'var(--dv3)' : n.kind === 'docker-host' ? 'var(--dv3)' : vm ? 'var(--dv1)' : 'var(--dv4)';
      const boxes = [{ u: -.1, v: -.1, z: -.12, w: Wd + .2, d: Dd + .2, h: .12, col: 'var(--s3)' },                    // the rails
        { u: 0, v: 0, z: 0, w: Wd, d: Dd, h: Hh, col: down ? 'var(--s3)' : 'var(--surf3)' },                          // the chassis
        { u: .1, v: .1, z: Hh, w: Wd - .2, d: .28, h: .07, col: lid }];                                                // the lid's front stripe
      for (let i = 0; i < 4; i++) boxes.push({ u: .3 + i * .62, v: Dd, z: .16, w: .44, d: .12, h: Hh * .45, col: i < lit ? (n.temp != null && n.temp >= 70 ? 'var(--ac3)' : 'var(--dv3)') : 'var(--bd2)' });   // drive bays on the front
      boxes.push({ u: Wd - .42, v: Dd, z: Hh * .62, w: .18, d: .1, h: .16, col: SC[n.status] });                     // the power lamp
      if (vm) boxes.push({ u: .18, v: .18, z: Hh, w: Wd - .36, d: Dd - .36, h: .16, col: 'color-mix(in srgb,var(--t1) 9%,transparent)' });   // the hypervisor's layer
      // trouble shows from across the room: a beacon on the lid (red when down or carrying findings, amber when warning),
      // red rails under a machine that is down; the caption says which and a badge stands over it
      const top = Hh + (vm ? .16 : 0), bcol = down || n.errors ? 'var(--ac4)' : n.status === 'warn' ? 'var(--ac3)' : '';
      if (bcol) boxes.push({ u: Wd / 2 - .26, v: Dd / 2 - .26, z: top, w: .52, d: .52, h: .1, col: 'var(--s3)' }, { u: Wd / 2 - .17, v: Dd / 2 - .17, z: top + .1, w: .34, d: .34, h: .62, col: bcol });
      if (down) boxes[0].col = 'var(--ac4)';
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col: bcol || lid, beacon: bcol ? s.at(Wd / 2, Dd / 2, top + .8) : null }; },
    // a client: a screen on a stand with a keyboard before it, the screen lit by who it is (agent, person, editor, door device, machine)
    client: (g, k, n) => { const col = n.status === 'down' ? 'var(--ac4)' : ({ agent: 'var(--dv1)', person: 'var(--ac)', editor: 'var(--dv3)', door: 'var(--dv2)' })[n.ckind] || 'var(--dv5)';
      const boxes = [{ u: -.9, v: -.5, z: 0, w: 1.8, d: 1, h: .1, col: 'var(--s3)' }, { u: -.12, v: -.12, z: .1, w: .24, d: .24, h: .7, col: 'var(--bd2)' },
        { u: -1.4, v: -.16, z: .8, w: 2.8, d: .2, h: 1.7, col: 'var(--surf3)' }, { u: -1.26, v: .04, z: .94, w: 2.52, d: .04, h: 1.42, col },
        { u: -1.1, v: .7, z: 0, w: 2.2, d: .62, h: .08, col: 'var(--s3)' }];
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col }; },
    // memory: six sticks standing on a board, lit by the share in use
    mem: (g, k, n) => { const pct = Math.max(0, Math.min(100, n.mem_pct || 0)), lit = Math.round(pct / 100 * 6), c = pct >= 90 ? 'var(--ac4)' : pct >= 75 ? 'var(--ac3)' : 'var(--dv6)', boxes = [{ u: -1.6, v: -.7, z: 0, w: 3.2, d: 1.4, h: .14, col: 'var(--s3)' }];
      for (let i = 0; i < 6; i++) boxes.push({ u: -1.4 + i * .5, v: -.45, z: .14, w: .24, d: .9, h: 1.5, col: i < lit ? c : 'var(--bd2)' });
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col: c }; },
    // history: the last eight readings as columns, the newest lit
    bars: (g, k, n) => { const ser = (S.snap.series || {})[n.id] || {}, vals = (ser.tps || ser.cpu || ser.ping || []).slice(-8), hi = Math.max.apply(null, vals.concat([1])), boxes = [{ u: -2.1, v: -.6, z: 0, w: 4.2, d: 1.2, h: .12, col: 'var(--s3)' }];
      vals.forEach((v, i) => boxes.push({ u: -1.95 + i * .5, v: -.3, z: .12, w: .36, d: .6, h: .15 + (v / hi) * 2.4, col: i === vals.length - 1 ? 'var(--ac)' : 'var(--dv3)' }));
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col: 'var(--dv3)' }; },
    // latency: its requests' times as a histogram, quick to slow
    hist: (g, k, n) => { const ms = n.latency || [], B = 8, lo = Math.min.apply(null, ms.concat([0])), hi = Math.max.apply(null, ms.concat([1])), r = (hi - lo) || 1, bins = new Array(B).fill(0);
      ms.forEach((v) => { bins[Math.min(B - 1, Math.floor((v - lo) / r * B))]++; }); const top = Math.max.apply(null, bins.concat([1])), boxes = [{ u: -2.1, v: -.6, z: 0, w: 4.2, d: 1.2, h: .12, col: 'var(--s3)' }];
      bins.forEach((c, i) => boxes.push({ u: -1.95 + i * .5, v: -.3, z: .12, w: .36, d: .6, h: .08 + c / top * 2.2, col: i < 4 ? 'var(--ac2)' : i < 6 ? 'var(--dv4)' : 'var(--ac3)' }));
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col: 'var(--dv4)' }; },
    // registration: five pillars - SSH login, directory, mesh door, certificate, backup - tall and green when in place
    shield: (g, k, n) => { const r = (n.reg && n.reg.planes) || {}, boxes = [{ u: -1.5, v: -.6, z: 0, w: 3, d: 1.2, h: .14, col: 'var(--s3)' }]; let yes = 0, of = 0;
      REGP.forEach((pn, i) => { const st = (r[pn] || {}).state, y = regYes(st), na = !st || st === 'n/a'; if (!na){ of++; if (y) yes++; }
        boxes.push({ u: -1.3 + i * .56, v: -.25, z: .14, w: .38, d: .5, h: na ? .15 : y ? 1.6 : .55, col: na ? 'var(--bd2)' : y ? 'var(--ac2)' : 'var(--ac4)' }); });
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col: of && yes === of ? 'var(--ac2)' : yes * 2 >= of ? 'var(--ac3)' : 'var(--ac4)' }; },
    // a GPU card on its bracket, two fans lit while it works
    gpu: (g, k, n) => { const busy = (n.inflight || 0) > 0, down = n.status === 'down', boxes = [{ u: -1.8, v: -.3, z: 0, w: 3.6, d: .6, h: .2, col: 'var(--s3)' }, { u: -1.7, v: -.2, z: .2, w: 3.4, d: .4, h: 1.5, col: down ? 'var(--s3)' : 'var(--surf3)' }];
      [-1.0, .35].forEach((u) => boxes.push({ u, v: .2, z: .45, w: .9, d: .06, h: .9, col: down ? 'var(--ac4)' : busy ? 'var(--ac)' : 'var(--bd2)' }));
      boxes.push({ u: -1.7, v: -.2, z: 1.7, w: 3.4, d: .4, h: .08, col: 'var(--dv1)' });
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col: down ? 'var(--ac4)' : busy ? 'var(--ac)' : 'var(--dv1)' }; },
    // models: one cartridge per model it holds, stacked
    models: (g, k, n) => { const c = Math.min(8, (n.caps || []).length), boxes = [{ u: -1, v: -1, z: 0, w: 2, d: 2, h: .12, col: 'var(--s3)' }];
      for (let i = 0; i < c; i++) boxes.push({ u: -.8, v: -.8, z: .12 + i * .26, w: 1.6, d: 1.6, h: .2, col: i % 2 ? 'var(--dv1)' : 'var(--dv5)' });
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col: 'var(--dv1)' }; },
    // a database: three platters, the top one lit while it works, red when it is down
    db: (g, k, n) => { const down = n.status === 'down', lit = (n.inflight || 0) > 0, boxes = [];
      for (let i = 0; i < 3; i++) boxes.push({ u: -1.1, v: -1.1, z: i * .75, w: 2.2, d: 2.2, h: .6, col: down ? 'var(--ac4)' : i === 2 && lit ? 'var(--ac)' : 'var(--surf3)' }, { u: -1.15, v: -1.15, z: i * .75 + .6, w: 2.3, d: 2.3, h: .06, col: 'var(--dv6)' });
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col: down ? 'var(--ac4)' : 'var(--dv6)' }; },
    // a queue: eight slots, filled by the work in flight through it
    queue: (g, k, n) => { const c = Math.min(8, n.inflight || 0), boxes = [{ u: -2, v: -.5, z: 0, w: 4, d: 1, h: .16, col: 'var(--s3)' }];
      for (let i = 0; i < 8; i++) boxes.push({ u: -1.85 + i * .48, v: -.3, z: .16, w: .36, d: .6, h: i < c ? .5 : .08, col: i < c ? 'var(--ac)' : 'var(--bd2)' });
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col: c ? 'var(--ac)' : 'var(--t3)' }; },
    // network: a switch, a port light per pipe (or per connection, for a client)
    nic: (g, k, n) => { const d = Math.min(8, n.conns || degOf(n.id)), boxes = [{ u: -1.8, v: -.6, z: 0, w: 3.6, d: 1.2, h: .55, col: 'var(--surf3)' }];
      for (let i = 0; i < 8; i++) boxes.push({ u: -1.6 + i * .42, v: .6, z: .18, w: .26, d: .06, h: .2, col: i < d ? 'var(--ac2)' : 'var(--s3)' });
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col: 'var(--dv3)' }; },
    dial: (g, k, n) => { const N = 20, R = 2.0, pct = (n.load || 0) / 100, lit = Math.max(1, Math.round(N * pct)), boxes = [];
      for (let i = 0; i < N; i++){ const a = Math.PI * .75 + (i / (N - 1)) * Math.PI * 1.5, on = i < lit; boxes.push({ u: Math.cos(a) * R - .17, v: Math.sin(a) * R - .17, z: 0, w: .34, d: .34, h: on ? .3 : .1, col: !on ? 'var(--s3)' : (i < N * .6 ? 'var(--ac2)' : i < N * .85 ? 'var(--ac3)' : 'var(--ac4)') }); }
      boxes.push({ u: -.36, v: -.36, z: 0, w: .72, d: .72, h: .4, col: 'var(--s3)' });
      const s = sceneG(g, boxes, stackKey, k); const an = Math.PI * .75 + ((lit - 1) / (N - 1)) * Math.PI * 1.5, a = s.at(0, 0, .44), b2 = s.at(Math.cos(an) * R * .8, Math.sin(an) * R * .8, .32);
      return { faces: s.faces, col: pct >= .85 ? 'var(--ac4)' : pct >= .6 ? 'var(--ac3)' : 'var(--ac2)', needle: ISO.segs([a, b2])[0] }; },
    tank: (g, k, n) => { const HH = 3.2, lvl = Math.max(.06, (n.disk_pct != null ? n.disk_pct : (n.load || 0)) / 100);
      const s = sceneG(g, [{ u: -.1, v: -.1, z: -.14, w: 3.2, d: 3.2, h: .14, col: 'var(--s3)', key: 0 }, { u: .08, v: .08, z: 0, w: 2.84, d: 2.84, h: lvl * HH, col: lvl > .9 ? 'var(--ac4)' : 'var(--dv6)', key: 1 }, { u: 0, v: 0, z: 0, w: 3, d: 3, h: HH, col: 'color-mix(in srgb,var(--t1) 7%,transparent)', key: 3 }], (b) => b.key * 1000 + b.z, k);
      return { faces: s.faces, col: lvl > .9 ? 'var(--ac4)' : 'var(--dv6)' }; },
    thermo: (g, k, n) => { const TB = 2.6, t0 = n.temp || 40, rows = [['gpu', t0], ['cpu', Math.max(30, t0 - 7)], ['nvme', Math.max(28, t0 - 12)]];
      const s = sceneG(g, rows.flatMap((r, i) => { const u = i * 1.2, h = r[1] / 95 * TB; return [{ u, v: 0, z: 0, w: .62, d: .62, h: .28, col: 'var(--s3)' }, { u: u + .09, v: .09, z: .06, w: .44, d: .44, h: .28, col: tc(r[1]) }, { u: u + .15, v: .15, z: .28, w: .32, d: .32, h: TB, col: 'color-mix(in srgb,var(--t1) 9%,transparent)' }, { u: u + .15, v: .15, z: .28, w: .32, d: .32, h: Math.max(.06, h), col: tc(r[1]) }, { u: u + .13, v: .13, z: TB + .28, w: .36, d: .36, h: .06, col: 'var(--bd2)' }]; }), (b) => b.u + b.v + b.z, k);
      return { faces: s.faces, col: tc(t0) }; },
    tower: (g, k, n) => { const ld = n.load != null ? n.load : (n.status === 'run' ? 60 : n.status === 'down' ? 0 : 25), h = .35 + ld / 100 * 2.8, col = n.status === 'down' ? 'var(--s3)' : tc(n.temp != null ? n.temp : ld * .9);
      const boxes = [{ u: -.9, v: -.9, z: 0, w: 1.8, d: 1.8, h: .18, col: 'var(--s3)' }, { u: -.5, v: -.5, z: .18, w: 1, d: 1, h, col }].concat(n.errors ? [{ u: -.12, v: -.12, z: .18 + h, w: .24, d: .24, h: .22, col: 'var(--ac4)' }] : []);
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col }; },
    crates: (g, k, n) => { const total = n.kind === 'docker' ? Math.min(12, Math.max(1, Math.round((parseInt(String(n.detail).split(' of ')[1]) || 1) / 24))) : n.kind === 'sandbox' ? 1 : Math.max(1, Math.min(12, n.inflight || 1));
      const up = n.kind === 'docker' ? Math.round(total * ((n.load || 0) / 100)) : total, boxes = [{ u: -1.5, v: -1.1, z: -.12, w: 3, d: 2.2, h: .12, col: 'var(--s3)' }];
      for (let i = 0; i < total; i++){ const c = i % 3, r = Math.floor(i / 3) % 2, lv = Math.floor(i / 6); boxes.push({ u: -1.4 + c * .95, v: -1 + r * 1.0, z: lv * .85, w: .8, d: .8, h: .8, col: i < up ? (n.status === 'down' ? 'var(--ac4)' : 'var(--dv3)') : 'var(--s3)' }); }
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col: n.status === 'down' ? 'var(--ac4)' : 'var(--dv3)' }; },
    chip: (g, k, n) => { const lit = Math.min(8, n.inflight || 0), boxes = [{ u: -1.3, v: -1.3, z: 0, w: 2.6, d: 2.6, h: .22, col: 'var(--s3)' }];
      for (let i = 0; i < 8; i++){ const c = i % 4, r = Math.floor(i / 4); boxes.push({ u: -1.05 + c * .6, v: -.9 + r * 1.1, z: .22, w: .38, d: .5, h: i < lit ? .3 : .1, col: i < lit ? 'var(--ac)' : 'var(--bd2)' }); }
      boxes.push({ u: .95, v: .95, z: .22, w: .22, d: .22, h: .26, col: SC[n.status] });
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col: lit ? 'var(--ac)' : SC[n.status] }; },
    mast: (g, k, n) => { const rssi = parseFloat(String(n.detail).split(' ')[0]); const bars = isNaN(rssi) ? (n.status === 'down' ? 0 : 2) : rssi >= -60 ? 3 : rssi >= -75 ? 2 : 1;
      const boxes = [{ u: -.7, v: -.7, z: 0, w: 1.4, d: 1.4, h: .2, col: 'var(--s3)' }, { u: -.12, v: -.12, z: .2, w: .24, d: .24, h: 3.2, col: 'var(--bd2)' }];
      [1.4, 1.0, .6].forEach((w, i) => { boxes.push({ u: -w / 2, v: -w / 2, z: 1.1 + i * .9, w, d: w, h: .12, col: i < bars ? 'var(--dv2)' : 'var(--s3)' }); });
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col: bars >= 3 ? 'var(--dv2)' : bars === 2 ? 'var(--ac3)' : 'var(--ac4)' }; },
    conveyor: (g, k, n) => { const c = Math.min(6, n.inflight || 0), boxes = [{ u: -2.2, v: -.55, z: 0, w: 4.4, d: 1.1, h: .22, col: 'var(--s3)' }, { u: -2.3, v: -.65, z: -.1, w: 4.6, d: 1.3, h: .1, col: 'var(--bd2)' }];
      for (let i = 0; i < c; i++) boxes.push({ u: -1.9 + i * .72, v: -.3, z: .22, w: .55, d: .6, h: .55, col: n.status === 'warn' ? 'var(--ac3)' : 'var(--ac)' });
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col: c ? 'var(--ac)' : 'var(--t3)' }; },
    stack: (g, k, n) => { const col = n.status === 'warn' ? 'var(--ac3)' : n.status === 'down' ? 'var(--ac4)' : 'var(--dv4)', boxes = [{ u: -1, v: -1, z: 0, w: 2, d: 2, h: .16, col: 'var(--s3)' }];
      [0, 1, 2].forEach((i) => boxes.push({ u: -.7 + i * .12, v: -.7 + i * .12, z: .16 + i * .5, w: 1.4 - i * .24, d: 1.4 - i * .24, h: .5, col: i === 2 && n.status === 'warn' ? 'var(--ac3)' : col }));
      const s = sceneG(g, boxes, stackKey, k); return { faces: s.faces, col }; }
  };
  lay.shown.forEach((n) => { const N = L[n.id]; if (!N) return; if (S.focus && n.plane !== S.focus) return;
    // the widgets stand in one line beside the pin, along the plane's lanes, each as far from the last as their sizes ask
    let run = 0.42 * Math.min(1, SP);
    wspec(n).forEach((w, i) => { const st = wstep(w); run += st / 2;
      const gu = N.u + run, gv = N.v + 0.08 * SP, g = Pg(gu, gv, N.z), k = 28 * ws * (w.s || 1); run += st / 2;
      const b = w.k === 'term' ? termFrame(g, k, n) : (BUILD[w.k] || BUILD.chip)(g, k, n); const bb = bboxOf(b.faces); out.boxes.push(bb);
      if (b.frame) out.frames.push(Object.assign({ id: n.id }, b.frame));
      out.groups.push({ id: n.id, key: n.id + ':' + i + ':' + w.k, i, a: out.faces.length, b: out.faces.length + b.faces.length, k: N.z * 1000 + gu + gv });
      b.faces.forEach((q) => out.faces.push(q));
      if (i === 0 && (n.status === 'down' || n.errors || (b.beacon && n.status === 'warn'))){ const at = b.beacon || [(bb.x0 + bb.x1) / 2, bb.y0];
        out.boxes.push({ x0: at[0] - 30, x1: at[0] + 30, y0: at[1] - 20, y1: at[1] });
        out.alarms.push({ id: n.id, x: at[0], y: at[1] - 2, sev: n.status === 'down' ? 'down' : n.errors ? 'err' : 'warn', t: n.status === 'down' ? 'DOWN' : n.errors ? n.errors + (n.errors === 1 ? ' finding' : ' findings') : 'warning' }); }
      // the first widget says its reading; the readings beside it speak when the node is selected (their titles always)
      // every widget says its value on a tag below it (the first, in widgets-only mode, also names its node)
      out.labels.push({ id: n.id, x: g[0], y: bb.y1 + 4, n: (S.lbl === 'widgets' && i === 0 ? (n.label.length > 22 ? n.label.slice(0, 20) + '…' : n.label) + ' · ' : '') + wcaption(n, w.k), col: b.col });
      out.hits.push({ x: bb.x0 - 3, y: bb.y0 - 3, w: bb.x1 - bb.x0 + 6, h: bb.y1 - bb.y0 + 6, id: n.id, k: w.k, title: WNAME[w.k] + ' on ' + n.label + ' · ' + wcaption(n, w.k) });
      if (b.needle) out.needles.push(b.needle);
      // each widget is joined to the node's pin along the plane's lanes
      ISO.segs([g, Pg(N.u, gv, N.z), Pg(N.u, N.v, N.z)]).forEach((sg) => { if (sg.len < .6) return; out.edges.push({ id: n.id, x: sg.x, y: sg.y, len: sg.len, deg: sg.deg, col: PCOL[n.plane] }); }); }); });
  return out;
}

// a face (x, y, w, h and a clip-path polygon in local px) as an svg polygon in plot px
const polyOf = (q) => { const x0 = parseFloat(q.x), y0 = parseFloat(q.y); const pts = String(q.cp).slice(8, -1).split(',').map((pt) => { const ab = pt.trim().split(/\s+/); return (x0 + parseFloat(ab[0])).toFixed(1) + ',' + (y0 + parseFloat(ab[1])).toFixed(1); }).join(' ');
  return '<polygon points="' + pts + '"' + (q.k === 't' ? ' class="t"' : '') + ' style="fill:' + q.col + '"/>'; };
// the view never leaves the picture: at least 80 px of it stays in the window whatever the pan and zoom
function clampView(){ const plot = $('plot'); if (!plot || !S.cw) return; const PW = plot.clientWidth || 800, PH = plot.clientHeight || 600, cw = S.cw * S.vz, ch = S.ch * S.vz, m = 80;
  S.vx = clamp(S.vx, Math.min(m - cw, 0), Math.max(PW - m, 0)); S.vy = clamp(S.vy, Math.min(m - ch, 0), Math.max(PH - m, 0)); }
function zoomAt(f, cx, cy){ const z0 = S.vz, z1 = clamp(z0 * f, .05, 4); S.vx = cx - (cx - S.vx) * (z1 / z0); S.vy = cy - (cy - S.vy) * (z1 / z0); S.vz = z1; }
function applyZoom(){ clampView(); const v = $('pzv'); v.style.transform = 'translate(' + S.vx + 'px,' + S.vy + 'px) scale(' + S.vz + ')'; v.style.setProperty('--ui', uiScale(S.vz).toFixed(3)); v.style.setProperty('--uic', (clamp(S.vz, 1, UI_MAX) / S.vz).toFixed(3)); /* a card reads at its own size whatever the zoom */ { const zl = $('pzLbl'); if (zl) zl.textContent = Math.round(S.vz * 100) + '%'; } }
// ── a pipe's look: a lit route (hot / faded), the active routes (hot / all but gone), the network lens ──
function pipeCls(l, RT, AS){ const hot = RT && RT.any && RT.edges[l.a + '>' + l.b];
  if (RT && RT.any) return (hot ? 'hot' : 'q2');
  if (S.rt) return S.rt !== l.a && S.rt !== l.b ? 'q' : '';
  if (AS) return AS.edges[l.a + '>' + l.b] ? 'hot' : 'q3';
  return S.lens === 'net' && l.kind !== 'mesh' && l.kind !== 'req' ? 'q2' : ''; }
// the routes carrying requests now: for each request in flight, the shortest run of pipes between its two ends
let ACT = null, ACT_OF = null;
function activeSet(ID){ if (ACT_OF === S.snap && ACT) return ACT; const edges = {}, nodes = {}, adj = {};
  (S.snap.links || []).forEach((l) => { if (!ID[l.a] || !ID[l.b]) return; (adj[l.a] = adj[l.a] || []).push(l); (adj[l.b] = adj[l.b] || []).push(l); });
  (S.snap.inflight || []).forEach((f) => { if (!ID[f.a] || !ID[f.b]) return; const prev = {}, seen = {}, q = [f.a]; seen[f.a] = 1;
    while (q.length && !seen[f.b]){ const x = q.shift(); (adj[x] || []).forEach((l) => { const y = l.a === x ? l.b : l.a; if (!seen[y]){ seen[y] = 1; prev[y] = l; q.push(y); } }); }
    if (!seen[f.b]) return; let x = f.b; nodes[x] = 1; while (x !== f.a){ const l = prev[x]; edges[l.a + '>' + l.b] = 1; x = l.a === x ? l.b : l.a; nodes[x] = 1; } });
  (S.snap.nodes || []).forEach((n) => { if (n.inflight > 0) nodes[n.id] = 1; });
  ACT_OF = S.snap; ACT = { edges, nodes, n: (S.snap.inflight || []).length }; return ACT; }
// ── routes: from a node, the shortest run to every end it reaches - down its links to the metal, up them to the work ──
function routesFrom(sel, links, ID){ if (!sel || !ID[sel]) return null;
  const out = {}, inn = {};
  links.forEach((l) => { if (!ID[l.a] || !ID[l.b] || !S.on[ID[l.a].plane] || !S.on[ID[l.b].plane]) return; (out[l.a] = out[l.a] || []).push(l); (inn[l.b] = inn[l.b] || []).push(l); });
  const walk = (adj, far) => { const prev = {}, seen = {}, d = {}, q = [sel], ends = []; seen[sel] = 1; d[sel] = 0;
    while (q.length){ const x = q.shift(), nx = (adj[x] || []).filter((l) => !seen[far(l)]);
      if (x !== sel && (!(adj[x] || []).length || d[x] >= 8)){ ends.push(x); continue; }
      nx.forEach((l) => { const y = far(l); if (seen[y]) return; seen[y] = 1; prev[y] = l; d[y] = d[x] + 1; q.push(y); }); }
    return ends.map((e) => { const path = []; let x = e; while (x !== sel){ const l = prev[x]; path.unshift(l); x = l.a === x ? l.b : l.a; } return { end: e, path }; }); };
  const down = walk(out, (l) => l.b), up = walk(inn, (l) => l.a), edges = {}, nodes = {};
  down.concat(up).forEach((r) => r.path.forEach((l) => { edges[l.a + '>' + l.b] = 1; nodes[l.a] = 1; nodes[l.b] = 1; }));
  return { down, up, edges, nodes, any: down.length + up.length > 0 }; }
// fit: scale the view so the whole lattice is on screen (never above 100%), centred in the stage
function fitView(all){ const plot = $('plot'); if (!plot) return; const PW = plot.clientWidth || 800, PH = plot.clientHeight || 600, cw = S.cw || PW, ch = S.ch || PH;
  if (S.view === '3d' || all){ S.vz = clamp(Math.min(1, (PW - 16) / cw, (PH - 16) / ch), all ? 0.05 : 0.25, 1); /* a new view: never below 25% (fit: everything) */ S.vx = (PW - cw * S.vz) / 2; S.vy = Math.max(0, (PH - ch * S.vz) / 2); }
  else { S.vz = clamp(Math.min(1, (PW - 8) / cw), 0.7, 1); S.vx = Math.max(0, (PW - cw * S.vz) / 2); S.vy = 0; }
  applyZoom(); }
// a change of size, gap or spread keeps the zoom and the point in the middle of the window (it refitted: the widgets never
// looked bigger because everything else shrank with them)
function keepCentre(fn){ const pl = $('plot'), PW = pl.clientWidth || 800, PH = pl.clientHeight || 600;
  const fx = S.cw ? ((PW / 2 - S.vx) / S.vz) / S.cw : 0.5, fy = S.ch ? ((PH / 2 - S.vy) / S.vz) / S.ch : 0.5; fn();
  S.vx = PW / 2 - fx * S.cw * S.vz; S.vy = PH / 2 - fy * S.ch * S.vz; applyZoom(); }
// rotation swings the view about the planes' own centre: the pivot keeps its place on screen and the pan takes up the difference
function keepPivot(fn){ const pz = $('pzv'); pz.classList.add('rotating');   // no pan transition while measuring: it would be read mid-flight
  const m0 = $('pzv').querySelector('.pivot'), a = m0 && m0.getBoundingClientRect(); fn(); const m1 = $('pzv').querySelector('.pivot');
  if (a && m1){ const b = m1.getBoundingClientRect(); S.vx -= b.left - a.left; S.vy -= b.top - a.top; applyZoom(); }
  if (!S.drag || S.drag.mode !== 'rot'){ void pz.offsetWidth; pz.classList.remove('rotating'); } }
function vitHtml(n){ const b = []; if (n.load != null) b.push(['cpu', n.load]); if (n.mem_pct != null) b.push(['mem', n.mem_pct]); if (n.disk_pct != null) b.push(['disk', n.disk_pct]);
  return b.length ? '<span class="vit" title="' + esc(b.map((x) => x[0] + ' ' + Math.round(x[1]) + '%').join(' · ')) + '">' + b.map((x) => '<i style="height:' + Math.max(2, Math.min(100, x[1]) / 100 * 12).toFixed(0) + 'px;background:' + tc(x[1] * .8) + '"></i>').join('') + '</span>' : ''; }
function regDots(n){ const r = n.reg; if (!r || !r.planes) return '';
  return '<span class="regd" title="' + esc(REGP.map((k) => k + ': ' + ((r.planes[k] || {}).state || '—')).join(' · ')) + '">' + REGP.map((k) => { const st = (r.planes[k] || {}).state; return '<i class="' + (regYes(st) ? 'y' : !st || st === 'n/a' ? 'na' : 'n') + '"></i>'; }).join('') + '</span>'; }
function draw(opts){ opts = opts || {};
  const plot = $('plot'), v = $('pzv'); if (!S.snap) return null;
  const snap = S.snap, ID = byId(), is3d = S.view === '3d', fl = snap.inflight || [];
  v.textContent = '';
  // the map model: the plot is the stage's window and never grows; the picture is drawn at its natural size on the canvas
  // (#pzv, W x H) and the view pans and zooms over it. One way to move, nothing to scroll, nothing to lose.
  const PW = plot.clientWidth || 800, PH = plot.clientHeight || 600;
  let W = is3d ? PW : Math.max(PW, 116 + (snap.domains || []).length * 330), H = PH;
  let lay = layout(W, H);
  for (let i = 0; i < 3 && is3d && lay.G && (lay.G.height > H + 4 || lay.G.width > W + 4); i++){ W = Math.max(W, Math.ceil(lay.G.width)); H = Math.max(H, Math.ceil(lay.G.height)); lay = layout(W, H); }
  if (!is3d && lay.bands.length){ const lb = lay.bands[lay.bands.length - 1]; H = Math.max(H, Math.ceil(lb.y + lb.h + lay.AXIS + 12)); }
  v.style.width = W + 'px'; v.style.height = H + 'px'; S.cw = W; S.ch = H;
  const pos = lay.pos, shown = lay.shown;
  const parts = [];
  // entering: a new view (2D, 3D stacked or flat, one layer) builds plane by plane; afterwards a node or widget that was not
  // on the graph before enters on its own. A redraw of what is already there (a poll, a rotation) animates nothing.
  const ek = is3d ? '3d:' + S.layout3d : '2d', replay = ek !== S.enterKey;
  if (replay){ S.enterKey = ek; S.seenN = {}; S.seenW = {}; }
  const pIdx = {}; lay.PL.forEach((pl, i) => { pIdx[pl.id] = i; });
  const entD = {}, perPl = {}; shown.slice().sort((a, b) => pos[a.id].x - pos[b.id].x).forEach((n) => { if (S.seenN[n.id]) return;
    const k = perPl[n.plane] = (perPl[n.plane] || 0) + 1; entD[n.id] = replay ? Math.min(1.4, pIdx[n.plane] * 0.1 + 0.15 + k * 0.014) : 0; });
  const E = (id, extra) => entD[id] == null ? ['', ''] : [' ent', ';--ed:' + (entD[id] + (extra || 0)).toFixed(2) + 's'];
  const EL = () => ['', ''];   // pipes appear with the view (animating hundreds of them cost the build its frames)
  const EP = (id) => replay ? [' ent', ';--ed:' + ((pIdx[id] || 0) * 0.1).toFixed(2) + 's'] : ['', ''];
  lay.bands.forEach((b, i) => parts.push('<div class="band' + (replay ? ' ent' : '') + '" style="top:' + px(b.y) + ';height:' + px(b.h) + ';--pc:' + b.col + (replay ? ';--ed:' + (i * 0.1).toFixed(2) + 's' : '') + '"></div>'));
  // the floors as polygons of one svg that overflows the plot's box: a floor is never clipped by its own frame
  if (lay.floors.length) parts.push('<svg class="floors-svg" width="' + W + '" height="' + H + '">' + lay.floors.map((f) => '<polygon class="' + (S.focus && S.focus !== f.id ? 'dim' : S.focus === f.id ? 'focus' : '') + (f.rail ? ' rail' : '') + EP(f.id)[0] + '" data-floor="' + f.id + '" points="' + f.pts + '" style="--pc:' + f.col + EP(f.id)[1] + '"><title>Double-click to look at this layer on its own (again for every layer)</title></polygon>').join('') + '</svg>');
  lay.planeLabels.forEach((p, i) => parts.push('<div class="plabel ' + (S.focus && p.id && S.focus !== p.id ? 'dim' : '') + (replay ? ' ent' : '') + '" style="left:' + px(p.x) + ';top:' + px(p.y) + ';color:' + p.col + (replay ? ';--ed:' + (i * 0.1 + 0.2).toFixed(2) + 's' : '') + '">' + esc(p.t) + '</div>'));
  lay.domLabels.forEach((d) => parts.push('<div class="dlabel' + (replay ? ' ent' : '') + '" style="left:' + px(d.x) + ';top:' + px(d.y) + (replay ? ';--ed:.8s' : '') + '">' + esc(d.t) + '</div>'));
  const segsOf = (a, b) => { const pts = S.pipes && lay.route ? lay.route(a, b) : [{ x: pos[a].x, y: pos[a].y }, { x: pos[b].x, y: pos[b].y }]; const segs = [];
    for (let i = 1; i < pts.length; i++){ const dx = pts[i].x - pts[i - 1].x, dy = pts[i].y - pts[i - 1].y, len = Math.hypot(dx, dy); if (len < .6) continue; segs.push({ x: pts[i - 1].x, y: pts[i - 1].y, len, deg: Math.atan2(dy, dx) * 180 / Math.PI }); } return segs; };
  let nLinks = 0; const RT = routesFrom(S.rt, snap.links, ID), AS = S.active && !S.rt ? activeSet(ID) : null;
  if (env.pill) env.pill(RT, AS, ID);
  // 3D pipes go into the widgets' svg, cut into short pieces and sorted by depth with the widgets: a pipe passes behind a widget
  // nearer the viewer and in front of one further away - never through one it is not wired to. 2D (and direct chords) stay divs.
  const svgPipes = is3d && lay.G && S.pipes, pipeItems = [];
  const chunks = (pts, cls, lc, ed, li) => { const P3 = lay.G.P;
    for (let i = 1; i < pts.length; i++){ const a0 = pts[i - 1].w, b0 = pts[i].w; if (!a0 || !b0) continue;
      const dl = Math.hypot(b0[0] - a0[0], b0[1] - a0[1]); if (dl < 1e-6 && Math.abs(b0[2] - a0[2]) < 0.5) continue;
      const n = Math.max(1, Math.ceil(dl / 2.5)), zb = Math.min(a0[3], b0[3]);
      for (let j = 0; j < n; j++){ const t0 = j / n, t1 = (j + 1) / n, lerp = (t) => [a0[0] + (b0[0] - a0[0]) * t, a0[1] + (b0[1] - a0[1]) * t, a0[2] + (b0[2] - a0[2]) * t], A1 = lerp(t0), B1 = lerp(t1);
        const A2 = P3(A1[0], A1[1], A1[2]), B2 = P3(B1[0], B1[1], B1[2]);
        pipeItems.push({ k: zb * 1000 + (A1[0] + A1[1] + B1[0] + B1[1]) / 2, li, cls, st: '--lc:' + lc + ed, d: 'M' + A2.x.toFixed(1) + ' ' + A2.y.toFixed(1) + 'L' + B2.x.toFixed(1) + ' ' + B2.y.toFixed(1) }); } } };
  snap.links.forEach((l, li) => { if (!pos[l.a] || !pos[l.b]) return; if (is3d && S.focus && (ID[l.a].plane !== S.focus || ID[l.b].plane !== S.focus)) return;
    const dim = pipeCls(l, RT, AS); nLinks++;
    // a pipe between planes wears its origin plane's colour; within a plane, its kind's
    const lc = ID[l.a].plane !== ID[l.b].plane ? PCOL[ID[l.a].plane] : LCOL[l.kind];
    const el = EL(l.a, l.b);
    if (svgPipes){ chunks(lay.route(l.a, l.b), dim + el[0], lc, el[1], li); return; }
    segsOf(l.a, l.b).forEach((s) => parts.push('<div class="link ' + dim + el[0] + '" data-l="' + li + '" style="left:' + px(s.x) + ';top:' + px(s.y) + ';width:' + px(s.len) + ';transform:rotate(' + s.deg.toFixed(2) + 'deg);--lc:' + lc + el[1] + '"></div>')); });
  const late = replay ? ';--ed:' + (Math.min(1.4, lay.PL.length * 0.1 + 0.9)).toFixed(2) + 's' : '';
  if (svgPipes) lay.busPts.forEach((b) => { if (!b.w) return; pipeItems.push({ k: b.w[3] * 1000 + b.w[0] + b.w[1] + 0.01, h: '<polygon class="busd' + (replay ? ' ent' : '') + '" points="' + [[0, -4.5], [4.5, 0], [0, 4.5], [-4.5, 0]].map((d) => (b.x + d[0]).toFixed(1) + ',' + (b.y + d[1]).toFixed(1)).join(' ') + '" style="' + late.slice(1) + '"/>' }); });
  fl.slice(0, 12).forEach((f, fi) => { if (!pos[f.a] || !pos[f.b]) return; if (is3d && S.focus && (ID[f.a].plane !== S.focus || ID[f.b].plane !== S.focus)) return;
    const segs = segsOf(f.a, f.b), total = segs.reduce((t, s) => t + s.len, 0) || 1, cycle = 1.6 + (fi % 4) * .5; let run = 0;
    segs.forEach((s) => { parts.push('<div class="flow' + (replay ? ' ent' : '') + '" style="' + late + ';left:' + px(s.x) + ';top:' + px(s.y) + ';transform:rotate(' + s.deg.toFixed(2) + 'deg);--len:' + px(s.len) + ';--dur:' + (cycle * s.len / total).toFixed(2) + 's;--dl:' + (fi * .28 + cycle * run / total).toFixed(2) + 's"><i style="background:' + KCOL[f.kind] + ';box-shadow:0 0 8px ' + KCOL[f.kind] + '"></i></div>'); run += s.len; }); });
  { const ce = $('counts'); if (ce) ce.textContent = shown.length + ' nodes · ' + nLinks + ' links'; }
  const chipAt = {}; let NSC = 1;
  if (is3d && lay.G){
    const wd = isoWidgets(lay, W, H); const wboxes = wd.boxes.slice();
    NSC = lay.G.FLAT ? Math.max(.68, Math.min(1, lay.G.fitS * 1.3)) : 1;
    wd.edges.forEach((e) => { const en = ['', '']; parts.push('<span class="wedge' + en[0] + '" style="left:' + px(e.x) + ';top:' + px(e.y) + ';width:' + px(e.len) + ';transform:rotate(' + e.deg.toFixed(2) + 'deg);--pc:' + e.col + en[1] + '"></span>'); });
    // every face as a polygon of one svg: the compositor paints one layer, not thousands of clipped boxes
    // one group per widget: it drops onto the plate when its node enters (one after the other), or on its own when it is added
    const items = wd.groups.map((gq) => { const nw = entD[gq.id] != null ? entD[gq.id] + 0.12 + gq.i * 0.07 : (!S.seenW[gq.key] && !replay ? 0 : null);
      return { k: gq.k, h: '<g data-n="' + esc(gq.id) + '"' + (nw != null || (AS && !AS.nodes[gq.id]) ? ' class="' + (nw != null ? 'ent' : '') + (AS && !AS.nodes[gq.id] ? ' wq' : '') + '"' : '') + (nw != null ? ' style="--ed:' + nw.toFixed(2) + 's"' : '') + '>' + wd.faces.slice(gq.a, gq.b).map(polyOf).join('') + '</g>' }; }).concat(pipeItems);
    items.sort((a, b) => a.k - b.k);
    // consecutive pieces of one pipe become one path: a few hundred elements, not thousands
    let svgH = '', run = null; const flush = () => { if (run){ svgH += '<path class="pipe ' + run.cls + '" data-l="' + run.li + '" d="' + run.d + '" style="' + run.st + '"/>'; run = null; } };
    items.forEach((x) => { if (x.d){ if (run && run.li === x.li && run.cls === x.cls) run.d += x.d; else { flush(); run = { li: x.li, cls: x.cls, st: x.st, d: x.d }; } } else { flush(); svgH += x.h; } }); flush();
    parts.push('<svg class="owf-svg" width="' + W + '" height="' + H + '">' + svgH + '</svg>');
    wd.needles.forEach((n) => parts.push('<span class="owf-n" style="left:' + px(n.x) + ';top:' + px(n.y) + ';width:' + px(n.len) + ';transform:rotate(' + n.deg.toFixed(1) + 'deg)"></span>'));
    // captions clear each other: front to back, a caption that meets one already placed steps down a line (twice), else it
    // stays in its widget's tooltip - dense rows no longer print over each other
    { const uiC = uiScale(S.vz), capPlaced = [];
      wd.labels.slice().sort((a, b) => b.y - a.y).forEach((l) => { const w = (String(l.n).length * 5.6 + 16) * uiC, h = 15 * uiC; let y = l.y, ok = false;
        for (let t = 0; t < 3 && !ok; t++){ const bx = { x0: l.x - w / 2, x1: l.x + w / 2, y0: y, y1: y + h };
          if (!capPlaced.some((b) => bx.x0 < b.x1 && bx.x1 > b.x0 && bx.y0 < b.y1 && bx.y1 > b.y0)){ capPlaced.push(bx); ok = true; } else y += h; }
        const en = ['', '']; if (ok) parts.push('<span class="owf-l' + en[0] + '" style="left:' + px(l.x) + ';top:' + px(y) + ';color:' + l.col + en[1] + '">' + esc(l.n) + '</span>'); }); }
    // trouble: a badge over the node's first widget - DOWN pulses; a click opens the node's card on its events
    wd.alarms.forEach((a) => { const en = E(a.id, 0.5); parts.push('<span class="owf-alarm ' + a.sev + en[0] + '" data-wnode="' + esc(a.id) + '" data-wkind="alarm" title="' + esc(((ID[a.id] || {}).label || a.id) + ' · ' + a.t + ' — click for its card') + '" style="left:' + px(a.x) + ';top:' + px(a.y) + en[1] + '">' + esc(a.t) + '</span>'); });
    // a terminal's screen carries its last lines; a click on any widget opens its node's card (a terminal's on its shell)
    wd.frames.forEach((f) => { const T = S.term[f.id], ls = T && T.lines.length ? T.lines.slice(-7) : [{ c: 'p', t: '$ _' }, { c: 'dim', t: 'click to open a shell' }];
      parts.push('<div class="owf-fr' + E(f.id, 0.4)[0] + '" data-wnode="' + esc(f.id) + '" data-wkind="term" title="A shell on ' + esc((ID[f.id] || {}).label || f.id) + ' — click to type" style="left:' + px(f.x) + ';top:' + px(f.y) + ';width:' + f.w + 'px;height:' + f.h + 'px;transform:' + f.tf + E(f.id, 0.4)[1] + '">' + ls.map((l) => '<span class="' + (l.c || '') + '">' + esc(String(l.t).slice(0, 64)) + '</span>').join('') + '</div>'); });
    wd.hits.forEach((h) => { if (h.k === 'term') return; parts.push('<span class="owf-hit" data-wnode="' + esc(h.id) + '" data-wkind="' + esc(h.k) + '" title="' + esc(h.title) + ' — click to open its card, ⊕ on the node to add or change widgets" style="left:' + px(h.x) + ';top:' + px(h.y) + ';width:' + px(h.w) + ';height:' + px(h.h) + '"></span>'); });
    const RAISE0 = 22, dtf = window.VeraISO.discTf(lay.G.iso), ui = uiScale(S.vz);
    const placed = [];
    shown.slice().sort((a, b) => pos[b.id].y - pos[a.id].y).forEach((n) => { const p = pos[n.id], col = lensCol(n) || PCOL[n.plane], hidden = !!(S.focus && n.plane !== S.focus);
      const en = E(n.id); parts.push('<span class="pin ' + (hidden ? 'dim' : '') + (lensDim(n) ? ' ldim' : '') + en[0] + '" style="left:' + px(p.x) + ';top:' + px(p.y) + ';--pc:' + col + ';--dtf:' + dtf + en[1] + '"></span>');
      if (S.lbl === 'widgets' && wspec(n).length) return;   // widgets only: the widget names its node
      // the chip stands straight above its pin; it is lifted (in screen px, at most 64) only as far as the widgets over its column ask
      const hw = (14 + 5.4 * Math.min(26, n.label.length) + (n.errors ? 20 : 0) + (n.inflight ? 12 : 0) + (n.load != null || n.mem_pct != null || n.disk_pct != null ? 16 : 0)) / 2 * ui, hh = 22 * ui;
      const over = (r, b) => p.x + hw > b.x0 && p.x - hw < b.x1 && p.y - r * ui > b.y0 - 2 && p.y - r * ui - hh < b.y1 + 2;
      const hit = (r) => (r < 110 && wboxes.some((b) => over(r, b))) || (!hidden && placed.some((b) => over(r, b)));
      let raise = RAISE0; while (hit(raise) && raise < 120) raise += 8;
      if (!hidden) placed.push({ x0: p.x - hw, x1: p.x + hw, y0: p.y - raise * ui - hh, y1: p.y - raise * ui });
      chipAt[n.id] = { x: p.x, y: p.y, r: raise };
      parts.push('<span class="stem ' + (hidden ? 'dim' : '') + en[0] + '" style="left:' + px(p.x) + ';top:' + px(p.y) + ';--r:' + raise + ';--pc:' + col + en[1] + '"></span>'); });
  }
  shown.forEach((n) => { if (is3d && lay.G && !chipAt[n.id]) return;
    const at = chipAt[n.id] || pos[n.id], busy = n.inflight > 0 || n.status === 'run', bb = chipAt[n.id] && chipAt[n.id].r != null;
    const spark = (S.snap.series[n.id] && (S.snap.series[n.id].tps || S.snap.series[n.id].cpu)) || null;
    const bars = spark ? spark.slice(-5) : null; const hi = bars ? Math.max.apply(null, bars.concat([1])) : 1;
    const lc = lensCol(n), lt = lensTag(n), en = E(n.id, is3d ? 0.18 : 0);
    parts.push('<div class="nd ' + (bb ? 'bb ' : '') + (n.status === 'down' ? 'isdown ' : '') + (en[0] ? 'ent ' : '') + (is3d && S.focus && n.plane !== S.focus ? 'dim ' : '') + (S.sel === n.id ? 'sel ' : '') + (lensDim(n) ? 'ldim ' : '') + ((RT && RT.nodes[n.id] && n.id !== S.rt) || (AS && AS.nodes[n.id]) ? 'onpath ' : '') + (busy ? 'busy ' : '') + (n.errors ? 'hasErr' : '') + '" data-node="' + esc(n.id) + '" title="' + esc(n.label + ' · ' + n.plane + ' · ' + n.detail + (n.inflight ? ' · ' + n.inflight + ' in flight' : '') + (n.errors ? ' · ' + n.errors + ' findings' : '') + (n.temp != null ? ' · ' + Math.round(n.temp) + '°' : '')) + '" style="left:' + px(at.x) + ';top:' + px(at.y) + ';--pc:' + (lc || PCOL[n.plane]) + ';--nsc:' + NSC.toFixed(2) + (bb ? ';--r:' + chipAt[n.id].r : '') + en[1] + '">'
      + '<span class="st" style="background:' + SC[n.status] + '"></span>' + esc(n.label.length > 26 ? n.label.slice(0, 24) + '…' : n.label)
      + (n.status === 'down' ? '<span class="dn">DOWN</span>' : '')
      + (lt ? '<span class="lz" style="--lzc:' + (lt.c || 'var(--t3)') + '">' + esc(lt.t) + '</span>' : '')
      + (bars ? '<span class="mini" style="color:' + PCOL[n.plane] + '">' + bars.map((v) => '<i style="height:' + Math.max(2, 11 * v / hi).toFixed(0) + 'px"></i>').join('') + '</span>' : '')
      + vitHtml(n) + (!is3d ? regDots(n) : '')
      + (n.temp != null ? '<span class="tmp" title="' + Math.round(n.temp) + '° · throttle at 83°"><i style="width:' + Math.min(100, n.temp / 90 * 100) + '%;background:' + tempCol(n.temp) + '"></i></span>' : '')
      + (n.inflight ? '<span class="lb">' + n.inflight + '</span>' : '') + (n.errors ? '<span class="err">' + n.errors + '</span>' : '')
      + '<span class="exp" data-exp="' + esc(n.id) + '" title="Open this node as a widget on the graph">⤢</span><span class="asg ' + (S.wpick === n.id ? 'on' : '') + '" data-asg="' + esc(n.id) + '" title="Add or change the iso widgets on this node">⊕</span></div>'); });
  (env.extraParts ? env.extraParts(ID, pos, NSC, W, H) : []).forEach((h) => { if (h) parts.push(h); });   /* the page's own: the widget picker, the node card */
  S.lastLay = { pos, W, H };
  if (lay.G && lay.G.pivot) parts.push('<i class="pivot" style="left:' + px(lay.G.pivot.x) + ';top:' + px(lay.G.pivot.y) + '"></i>');
  v.innerHTML = parts.join('');
  applyZoom(); if (env.afterDraw) env.afterDraw();
  shown.forEach((n) => { S.seenN[n.id] = 1; wspec(n).forEach((w, i) => { S.seenW[n.id + ':' + i + ':' + w.k] = 1; }); });
  // once the build is over the entry classes go: finished animations on thousands of elements cost the canvas its frame rate
  if (Object.keys(entD).length){ clearTimeout(S.entT); S.entT = setTimeout(() => { v.querySelectorAll('.ent').forEach((e) => e.classList.remove('ent')); }, 2400); }
  // a new view opens fitted (3D: the whole lattice; 2D: its width, from the top); a redraw keeps the view, never off screen
  if (replay) fitView(); else applyZoom();
  return lay; }

return { ROWS, SLOTS, slotsFor, tempCol, tc, WKINDS, WNAME, wstep, UI_MIN, UI_MAX, uiScale, REGP, regYes, degOf, regOf, wdefaults, wcaption,
  layout, isoWidgets, polyOf, clampView, zoomAt, applyZoom, draw, pipeCls, activeSet, routesFrom, fitView, keepCentre, keepPivot, vitHtml, regDots };
}

const CSS = ":host{--surf:color-mix(in srgb,var(--ac) 4%,var(--s1));--surf2:color-mix(in srgb,var(--ac) 6%,var(--s2));--surf3:color-mix(in srgb,var(--ac) 8%,var(--s3));\n  --elev:0 1px 2px rgba(0,0,0,.20),0 10px 30px -16px rgba(0,0,0,.60),inset 0 1px 0 color-mix(in srgb,var(--t1) 7%,transparent);\n  --elev-lo:0 1px 2px rgba(0,0,0,.14),inset 0 1px 0 color-mix(in srgb,var(--t1) 5%,transparent);\n  --glow:0 0 22px -6px color-mix(in srgb,var(--ac) 55%,transparent);--fill:color-mix(in srgb,var(--ac) 15%,transparent)}\n.plot{flex:1 1 auto;min-height:420px;position:relative;overflow:hidden;margin:0 var(--pad) var(--pad);border-radius:var(--r-sm);background:var(--bg)}\n.pzv{position:absolute;left:0;top:0;transform-origin:0 0;cursor:grab;transition:transform .38s cubic-bezier(.22,.68,.18,1);--ui:1}\n.pzv.dragging{transition:none!important;cursor:grabbing}.pzv.rotating{transition:none!important;cursor:move}\n@keyframes fl-in{from{opacity:0;transform:translateY(14px)}}\n@keyframes ndo-in{from{opacity:0;transform:translate(-50%,-50%) scale(.4)}to{opacity:1;transform:translate(-50%,-50%) scale(calc(var(--nsc,1) * var(--ui,1)))}}\n@keyframes lk-draw{from{clip-path:inset(0 100% 0 0)}to{clip-path:inset(0 0 0 0)}}\n@keyframes lb-in{from{opacity:0}}\n/* entering: a view builds plane by plane (floors rise, pins land, stems grow, chips pop, widgets drop onto the plate, pipes\n   draw last); later a new node or widget enters on its own. --ed is its delay. */\n@keyframes opsIn{from{opacity:0;translate:0 12px;scale:.7}}\n@keyframes opsFade{from{opacity:0}}\n@keyframes opsDrop{from{opacity:0;transform:translateY(-26px)}}\n@keyframes opsStem{from{clip-path:inset(100% 0 0 0)}}\n@keyframes opsFloor{from{opacity:0;transform:translateY(34px)}}\n@keyframes opsPin{from{opacity:0;scale:2.6}}\n@keyframes opsBand{from{opacity:0;transform:translateX(-18px)}}\n.nd.ent{animation:opsIn .55s cubic-bezier(.2,.9,.25,1.25) backwards;animation-delay:var(--ed,0s)}\n.pin.ent{animation:opsPin .5s ease-out backwards;animation-delay:var(--ed,0s)}\n.stem.ent{animation:opsStem .45s ease-out both;animation-delay:calc(var(--ed,0s) + .08s)}\n.owf-svg g.ent{animation:opsDrop .6s cubic-bezier(.2,.9,.3,1.2) backwards;animation-delay:var(--ed,0s)}\n.floors-svg polygon.ent{animation:opsFloor .7s cubic-bezier(.2,.8,.2,1) backwards;animation-delay:var(--ed,0s);transform-box:fill-box}\n.band.ent{animation:opsBand .55s cubic-bezier(.2,.8,.2,1) backwards;animation-delay:var(--ed,0s)}\n.flow.ent,.owf-l.ent,.owf-fr.ent,.owf-alarm.ent,.wedge.ent,.link.ent,.bus.ent,.plabel.ent,.dlabel.ent{animation:opsFade .6s ease-out backwards;animation-delay:var(--ed,0s)}\n/* trouble: a badge over the node's first widget (DOWN pulses), a ring round a down chip */\n@keyframes alarmPulse{0%{opacity:.95;transform:scale(1)}100%{opacity:0;transform:scale(1.7)}}\n.owf-alarm{position:absolute;z-index:10;transform:translate(-50%,-100%) scale(var(--ui,1));transform-origin:50% 100%;font-family:var(--f-mono);font-size:9px;font-weight:700;letter-spacing:.06em;color:#fff;background:var(--ac4);padding:1px 5px;border-radius:3px;cursor:pointer;white-space:nowrap;box-shadow:0 0 12px -2px var(--ac4)}\n.owf-alarm.warn{background:var(--ac3);color:#1a1300;box-shadow:0 0 10px -2px var(--ac3)}\n.owf-alarm.down::before{content:'';position:absolute;inset:-4px;border-radius:6px;border:1.5px solid var(--ac4);animation:alarmPulse 1.6s ease-out infinite;pointer-events:none}\n.nd.isdown::after{content:'';position:absolute;inset:-4px;border-radius:inherit;border:1.5px solid var(--ac4);animation:alarmPulse 1.6s ease-out infinite;pointer-events:none}\n.nd.isdown{box-shadow:inset 0 0 0 1.5px var(--ac4),0 0 16px -3px var(--ac4)}\n.nd .dn{font-family:var(--f-mono);font-size:8.5px;font-weight:700;color:#fff;background:var(--ac4);padding:0 4px;border-radius:3px;letter-spacing:.05em}\n.floor{position:absolute;pointer-events:none;background:color-mix(in srgb,var(--pc) 6%,transparent);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--pc) 26%,transparent);cursor:pointer}\n.floor.dim{display:none}.floor.focus{filter:brightness(1.25)}\n.band{position:absolute;left:0;right:0;pointer-events:none;background:color-mix(in srgb,var(--pc) 5%,transparent);border-top:1px solid color-mix(in srgb,var(--pc) 22%,transparent)}\n/* labels, pins and lines hold a screen size while the view zooms (--ui = the counter-scale) */\n.plabel{position:absolute;font-size:9px;letter-spacing:.13em;text-transform:uppercase;font-weight:600;color:var(--pc);pointer-events:none;opacity:.9;white-space:nowrap;transform:scale(var(--ui,1));transform-origin:0 0}\n.plabel.dim{display:none}\n.dlabel{position:absolute;font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--t3);pointer-events:none;transform:translateX(-50%) scale(var(--ui,1));transform-origin:50% 0}\n.link{position:absolute;height:calc(2px * var(--ui,1));border-radius:2px;transform-origin:0 50%;background:var(--lc,var(--bd2));pointer-events:none;opacity:.85;box-shadow:0 0 7px -1px var(--lc,transparent)}\n.link.q{opacity:.42;box-shadow:none}.plot:hover .link.q{opacity:.55}\n.flow{position:absolute;transform-origin:0 50%;height:0;pointer-events:none;z-index:8}\n.flow i{position:absolute;top:calc(-3px * var(--ui,1));left:0;width:calc(6px * var(--ui,1));height:calc(6px * var(--ui,1));border-radius:50%;animation:fl var(--dur,2.4s) linear infinite;animation-delay:var(--dl,0s)}\n@keyframes fl{from{transform:translateX(0);opacity:0}10%{opacity:1}90%{opacity:1}to{transform:translateX(var(--len));opacity:0}}\n.nd{position:absolute;transform:translate(-50%,-50%) scale(calc(var(--nsc,1) * var(--ui,1)));display:flex;align-items:center;gap:6px;height:24px;padding:0 9px;border-radius:var(--r-sm);background:var(--surf2);font-size:10px;white-space:nowrap;cursor:pointer;z-index:2;box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--pc) 45%,transparent)}\n.nd:hover{background:var(--s3);z-index:6}\n.nd.dim{display:none}\n.iso .nd{height:22px;padding:0 8px;gap:5px}\n.iso .nd .mini,.iso .nd .tmp,.iso .nd .lb,.iso .nd .exp,.iso .nd .asg{display:none}\n.iso .nd:hover .mini,.iso .nd.sel .mini,.iso .nd:hover .tmp,.iso .nd.sel .tmp,.iso .nd:hover .exp,.iso .nd.sel .exp,.iso .nd:hover .asg,.iso .nd.sel .asg{display:flex}\n.nd .st{width:6px;height:6px;border-radius:50%;flex-shrink:0}\n.nd.sel{box-shadow:0 0 0 2px var(--ac),var(--glow)}\n.nd.busy{box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--pc) 45%,transparent),0 0 14px -2px color-mix(in srgb,var(--ac) 55%,transparent)}\n.nd .lb{font-family:var(--f-mono);font-size:9px;color:var(--t3)}\n.nd .mini{display:flex;align-items:flex-end;gap:1px;height:11px;width:24px;flex-shrink:0}\n.nd .mini i{flex:1;border-radius:1px;background:currentColor;opacity:.5}\n.nd .tmp{width:20px;height:4px;border-radius:2px;background:var(--s3);overflow:hidden;flex-shrink:0}\n.nd .tmp i{display:block;height:100%;border-radius:2px}\n.nd .err{min-width:15px;height:15px;padding:0 4px;border-radius:8px;background:var(--ac4);color:var(--on-ac);font-family:var(--f-mono);font-size:8.5px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0}\n.nd.hasErr{box-shadow:inset 0 0 0 1px var(--ac4),0 0 14px -3px var(--ac4)}\n.nd .exp,.nd .asg{width:15px;height:15px;border-radius:3px;color:var(--t3);display:flex;align-items:center;justify-content:center;font-size:9px;flex-shrink:0;margin-left:2px}\n.nd .exp:hover,.nd .asg:hover,.nd .asg.on{color:var(--ac);background:var(--s3)}\n.flat .nd .asg{display:none}\n.pin{position:absolute;width:12px;height:12px;border-radius:50%;background:transparent;pointer-events:none;z-index:5;transform:translate(-50%,-50%) var(--dtf,none) scale(var(--ui,1));border:1.5px solid var(--pc);box-shadow:0 0 0 1px var(--bg),inset 0 0 0 1px var(--bg)}\n.pin::after{content:'';position:absolute;left:50%;top:50%;width:4px;height:4px;margin:-2px 0 0 -2px;border-radius:50%;background:var(--pc);box-shadow:0 0 0 1px var(--bg)}\n.stem{position:absolute;width:calc(1.5px * var(--ui,1));height:calc(var(--r,20) * var(--ui,1) * 1px);transform:translate(-50%,-100%);pointer-events:none;z-index:9;background:linear-gradient(0deg,color-mix(in srgb,var(--pc) 95%,transparent),color-mix(in srgb,var(--pc) 45%,transparent))}\n/* a billboard: the chip stands straight above its pin, the lift held in screen px while the view zooms */\n.nd.bb{transform-origin:50% 100%;transform:translate(-50%,-100%) translateY(calc(var(--r,20) * var(--ui,1) * -1px)) scale(calc(var(--nsc,1) * var(--ui,1)));z-index:11}\n.floors-svg polygon.rail{opacity:.5}\n.floors-svg{position:absolute;left:0;top:0;overflow:visible;z-index:0}\n.floors-svg polygon{fill:color-mix(in srgb,var(--pc) 6%,transparent);stroke:color-mix(in srgb,var(--pc) 26%,transparent);stroke-width:1;cursor:pointer;pointer-events:visiblePainted}\n.floors-svg polygon.focus{fill:color-mix(in srgb,var(--pc) 11%,transparent)}.floors-svg polygon.dim{display:none}\n.bus{position:absolute;width:calc(7px * var(--ui,1));height:calc(7px * var(--ui,1));border-radius:2px;transform:translate(-50%,-50%) rotate(45deg);background:var(--bg);box-shadow:0 0 0 calc(1.5px * var(--ui,1)) var(--pc);z-index:4;pointer-events:none}\n.wedge{position:absolute;height:calc(1.5px * var(--ui,1));transform-origin:0 50%;pointer-events:none;z-index:1;opacity:.9;background:repeating-linear-gradient(90deg,var(--pc) 0 4px,transparent 4px 7px)}\n.pin.dim,.stem.dim{display:none}\n.owf-svg{position:absolute;left:0;top:0;overflow:visible;z-index:7;pointer-events:none}\n.owf-svg polygon.t{stroke:rgba(255,255,255,.1);stroke-width:1}\n.owf-n{position:absolute;height:2.5px;transform-origin:0 50%;background:var(--t1);border-radius:2px;z-index:8;box-shadow:0 0 6px 1px color-mix(in srgb,var(--ac3) 60%,transparent)}\n.owf-l{position:absolute;transform:translate(-50%,0) scale(var(--ui,1));transform-origin:50% 0;font-family:var(--f-mono);font-size:9px;line-height:1.3;z-index:8;white-space:nowrap;pointer-events:none;padding:1px 5px;border-radius:3px;background:color-mix(in srgb,var(--bg) 82%,transparent);box-shadow:0 0 0 1px color-mix(in srgb,currentColor 28%,transparent)}\n.owf-hit{position:absolute;z-index:9;border-radius:6px;cursor:pointer}\n.pivot{position:absolute;width:0;height:0;pointer-events:none}\n.owf-svg .pipe{fill:none;stroke:var(--lc,var(--bd2));stroke-width:2;stroke-linecap:round;stroke-linejoin:round;opacity:.85}\n.owf-svg .pipe.q{opacity:.42}.owf-svg .pipe.q2{opacity:.12}.owf-svg .pipe.q3,.link.q3{opacity:.06;box-shadow:none}\n.owf-svg .pipe.hot{opacity:1;stroke-width:3.4}\n.owf-svg polygon.busd{fill:var(--bg);stroke:var(--t3);stroke-width:1.5}\n.owf-svg g.wq{opacity:.3}\n.owf-svg .pipe.ent,.owf-svg polygon.busd.ent{animation:opsFade .6s ease-out backwards;animation-delay:var(--ed,0s)}\n.nd .vit{display:inline-flex;align-items:flex-end;gap:1px;height:12px;flex:none}.nd .vit i{width:3px;border-radius:1px;display:block}\n.nd .regd{display:inline-flex;gap:2px;flex:none}.nd .regd i{width:5px;height:5px;border-radius:50%;display:block}\n.regd i.y{background:var(--ac2)}.regd i.n{background:var(--ac4)}.regd i.na{background:var(--s3)}\n.ndw.wide .b{max-height:440px;overflow:auto}\n.owf-fr{position:absolute;transform-origin:0 0;z-index:8;background:var(--bg);color:var(--t2);font-family:var(--f-mono);font-size:8px;line-height:1.35;padding:3px 5px;box-sizing:border-box;overflow:hidden;cursor:pointer;box-shadow:inset 0 0 0 1px var(--bd);display:flex;flex-direction:column;justify-content:flex-end;white-space:pre}\n.owf-fr span{overflow:hidden;text-overflow:ellipsis}.owf-fr .p{color:var(--ac2)}.owf-fr .err,.owf-fr .warn{color:var(--ac3)}.owf-fr .ok{color:var(--t3)}.owf-fr .dim{color:var(--t3)}\n.link.hot{opacity:1;height:calc(3px * var(--ui,1));box-shadow:0 0 9px 0 var(--lc,transparent);z-index:2}\n.link.q2{opacity:.12;box-shadow:none}\n.nd.onpath{box-shadow:0 0 0 1.5px var(--ac2),0 0 12px -3px var(--ac2)}\n.nd.ldim,.pin.ldim,.stem.ldim{opacity:.3}\n.nd .lz{font-family:var(--f-mono);font-size:9px;color:var(--lzc,var(--t2));padding:0 4px;border-radius:3px;background:color-mix(in srgb,var(--lzc,var(--t3)) 16%,transparent);white-space:nowrap}\n.ndw{position:absolute;transform:translate(-50%,-50%) scale(var(--uic,1));width:236px;border-radius:var(--ui-radius);background:var(--surf);box-shadow:var(--elev),0 0 0 1.5px var(--ac);z-index:30;overflow:hidden;isolation:isolate}\n.ndw.wide{width:330px}\n.ndw .h{display:flex;align-items:center;gap:8px;padding:10px 12px 0;font-size:11.5px;font-weight:600}\n.ndw .h .st{width:7px;height:7px;border-radius:50%;flex-shrink:0}\n.ndw .h .x{margin-left:auto;width:19px;height:19px;border-radius:var(--r-sm);color:var(--t3);display:flex;align-items:center;justify-content:center;font-size:11px}\n.ndw .h .x:hover{color:var(--t1);background:var(--s3)}\n.ndw .b{padding:8px 12px 12px;display:flex;flex-direction:column;gap:8px}";

// ── a self-contained view for any page: named planes and areas, in a shadow root wearing the plot's CSS ──
const PALETTE = ['var(--dv1)', 'var(--dv3)', 'var(--dv6)', 'var(--dv4)', 'var(--dv2)', 'var(--dv5)', 'var(--dv7)', 'var(--ac)'];
const LCOL0 = { req: 'var(--dv3)', data: 'var(--dv6)', runs: 'var(--dv1)', repo: 'var(--dv4)', mesh: 'var(--dv2)' };
const KCOL0 = { 'llm.generate': 'var(--dv1)', 'embed': 'var(--dv6)', 'cap call': 'var(--dv3)', 'fabric write': 'var(--dv2)', 'git op': 'var(--dv4)', 'background': 'var(--dv7)' };
const SC0 = { ok: 'var(--ac2)', run: 'var(--ac)', warn: 'var(--ac3)', down: 'var(--ac4)' };
function mount(host, o){
  o = o || {};
  const root = host.attachShadow ? (host.shadowRoot || host.attachShadow({ mode: 'open' })) : host;
  root.innerHTML = '<style>' + CSS + ':host{display:block;position:relative;width:100%;height:100%;overflow:hidden;font-family:var(--f-ui,system-ui,sans-serif);font-size:11.5px;color:var(--t1)}'
    + '.plot{position:absolute;inset:0;min-height:0;overflow:hidden;background:var(--bg)}.vlb{position:absolute;right:8px;top:8px;z-index:44;display:flex;gap:3px;padding:3px;border-radius:8px;background:color-mix(in srgb,var(--s1) 90%,transparent);box-shadow:0 0 0 1px var(--bd)}'
    + '.vlb button{font:inherit;font-size:10px;color:var(--t2);background:none;border:0;border-radius:5px;padding:3px 8px;cursor:pointer}.vlb button.on,.vlb button:hover{background:var(--s3);color:var(--t1)}</style>'
    + '<div class="plot iso" id="plot"><div class="pzv" id="pzv"></div><div class="vlb"><button data-v="3d">3D</button><button data-v="2d">2D</button><button data-l="stack">Stacked</button><button data-l="flat">Flat</button><button data-f="1">fit</button></div></div>';
  const q = (id) => root.getElementById ? root.getElementById(id) : root.querySelector('#' + id);
  const S = { view: o.view || '3d', layout3d: o.layout || 'stack', focus: null, pipes: true, widgets: o.widgets !== false, lbl: o.labels || 'both', sel: null, rt: null, active: false,
    on: {}, vz: 1, vx: 0, vy: 0, tilt: null, azim: null, zgap: 1, spread: 1, wsz: o.wsz || 1, wl: {}, term: {}, snap: null, seenN: {}, seenW: {}, enterKey: '', drag: null, moved: false };
  const PCOL = {};
  const E = engine({ S, $: q, PCOL, LCOL: Object.assign({}, LCOL0, o.linkColours || {}), KCOL: KCOL0, SC: SC0, wspec: o.widgetsFor || ((n) => E.wdefaults(n)) });
  const ID = () => { const m = {}; ((S.snap && S.snap.nodes) || []).forEach((n) => { m[n.id] = n; }); return m; };
  function set(d){ d = d || {};
    const planes = (d.planes || []).map((pl, i) => ({ id: String(pl.id != null ? pl.id : pl), name: pl.name || String(pl.id != null ? pl.id : pl), count: 0, col: pl.col }));
    planes.forEach((pl, i) => { PCOL[pl.id] = pl.col || PALETTE[i % PALETTE.length]; if (S.on[pl.id] == null) S.on[pl.id] = true; });
    const areas = (d.areas || ['all']).map(String), ax = {}; areas.forEach((a, i) => { ax[a] = i; });
    const nodes = (d.nodes || []).map((n) => { const a = typeof n.area === 'number' ? n.area : (ax[n.area] != null ? ax[n.area] : (typeof n.domain === 'number' ? n.domain : 0));
      return Object.assign({ status: 'ok', detail: '', inflight: 0, errors: 0, kind: 'node' }, n, { id: String(n.id), label: String(n.label != null ? n.label : n.id), plane: String(n.plane), domain: Math.max(0, Math.min(areas.length - 1, a)) }); })
      .filter((n) => PCOL[n.plane] != null);
    planes.forEach((pl) => { pl.count = nodes.filter((n) => n.plane === pl.id).length; });
    const have = {}; nodes.forEach((n) => { have[n.id] = 1; });
    S.snap = { planes, domains: areas, nodes, links: (d.links || []).filter((l) => have[l.a] && have[l.b]).map((l) => ({ a: String(l.a), b: String(l.b), kind: l.kind || 'req' })), inflight: d.inflight || [], series: d.series || {} };
    render(); }
  function render(){ if (!S.snap) return; E.draw({}); q('plot').classList.toggle('iso', S.view === '3d'); q('plot').classList.toggle('flat', S.view !== '3d');
    root.querySelectorAll('.vlb [data-v]').forEach((b) => b.classList.toggle('on', b.dataset.v === S.view));
    root.querySelectorAll('.vlb [data-l]').forEach((b) => { b.classList.toggle('on', b.dataset.l === S.layout3d); b.disabled = S.view !== '3d'; }); }
  const plot = q('plot'), v = q('pzv');
  const onWheel = (e) => { if (!(e.ctrlKey || e.metaKey) && o.wheel !== 'zoom') return; e.preventDefault(); const r = plot.getBoundingClientRect(); E.zoomAt(e.deltaY > 0 ? .88 : 1.14, e.clientX - r.left, e.clientY - r.top); E.applyZoom(); };
  plot.addEventListener('wheel', onWheel, { passive: false });
  plot.addEventListener('mousedown', (e) => { if (e.button || e.target.closest('.vlb')) return; S.moved = false; const rot = (e.ctrlKey || e.metaKey) && S.view === '3d';
    const T = S.tilt == null ? (S.layout3d === 'flat' ? 34 : 26) : S.tilt, A = S.azim == null ? 45 : S.azim; S.drag = { mode: rot ? 'rot' : 'pan', x0: e.clientX, y0: e.clientY, vx0: S.vx, vy0: S.vy, t0: T, a0: A }; e.preventDefault(); });
  let raf = 0;
  const onMove = (e) => { const d = S.drag; if (!d) return; if (Math.abs(e.clientX - d.x0) + Math.abs(e.clientY - d.y0) > 4) S.moved = true;
    if (d.mode === 'rot'){ S.azim = clamp(d.a0 - (e.clientX - d.x0) * 0.22, 26, 64); S.tilt = clamp(d.t0 + (e.clientY - d.y0) * 0.18, 4, 60); if (!raf) raf = requestAnimationFrame(() => { raf = 0; E.keepPivot(() => E.draw({ plotOnly: true })); }); return; }
    S.vx = d.vx0 + (e.clientX - d.x0); S.vy = d.vy0 + (e.clientY - d.y0); v.style.transform = 'translate(' + S.vx + 'px,' + S.vy + 'px) scale(' + S.vz + ')'; };
  const onUp = () => { if (S.drag){ S.drag = null; E.applyZoom(); } };
  window.addEventListener('mousemove', onMove); window.addEventListener('mouseup', onUp);
  root.addEventListener('click', (e) => { const t = e.target;
    const b = t.closest && t.closest('.vlb button'); if (b){ if (b.dataset.v){ S.view = b.dataset.v; render(); fit(); } else if (b.dataset.l){ S.layout3d = b.dataset.l; S.focus = null; render(); fit(); } else fit(true); return; }
    if (S.moved) return;
    const nd = t.closest && t.closest('[data-node],[data-wnode]'); if (nd){ const n = ID()[nd.dataset.node || nd.dataset.wnode]; S.sel = n ? n.id : null; S.rt = S.sel; render(); if (n && o.onPick) o.onPick(n); return; }
    if (S.rt){ S.rt = null; render(); } });
  root.addEventListener('dblclick', (e) => { const fl = e.target.closest && e.target.closest('[data-floor]'); if (fl && S.view === '3d'){ S.focus = S.focus === fl.dataset.floor ? null : fl.dataset.floor; render(); return; }
    const nd = e.target.closest && e.target.closest('[data-node],[data-wnode]'); if (nd && o.onOpen){ const n = ID()[nd.dataset.node || nd.dataset.wnode]; if (n) o.onOpen(n); } });
  // a fit wants a host with a size: until it has one the fit waits, and the first size it gets fits the view
  let fitWant = false; const sized = () => host.clientWidth > 0 && host.clientHeight > 0;
  const fit = (all) => { if (!sized()){ fitWant = true; return; } fitWant = false; E.fitView(!!all); };
  let ro = null; if (window.ResizeObserver){ ro = new ResizeObserver(() => { if (!S.snap || !sized()) return; render(); if (fitWant) fit(); }); ro.observe(host); }
  if (o.planes) set(o);
  return { set, render, fit, option(k, val){ S[k] = val; render(); }, state: S, engine: E,
    destroy(){ window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp); if (ro) ro.disconnect(); root.innerHTML = ''; } };
}
window.VeraLattice = { engine, mount, CSS };
})();
