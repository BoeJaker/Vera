// the ISO view's sector discs (the GraphViews board): one annular wedge per family on the floor, over the family's
// sector, squashed to the plate's ellipse, none in the other views
const path = require('path'); const vm = require('vm'); const fs = require('fs');
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'context_graph_element.js'), 'utf8');
const win = { customElements: { get: () => undefined, define: () => {} }, addEventListener: () => {}, document: { createElement: () => ({ style: {}, setAttribute: () => {}, appendChild: () => {} }), head: { appendChild: () => {} }, querySelectorAll: () => [] }, HTMLElement: class {}, location: { origin: '' }, requestAnimationFrame: (f) => 0 };
win.window = win; vm.createContext(win); vm.runInContext(src, win);
const G = win.VeraContextGraph; let f = 0; const ok = (c, m) => { console.log((c ? 'ok   ' : 'FAIL ') + m); if (!c) f++; };
const nodes = [{ id: 'v1', label: 'a.py', source: 'vector', type: 'chunk', score: .9 }, { id: 'c1', label: 'obs.x', source: 'cap', type: 'capability', score: .7 }, { id: 's1', label: 'fabric-ops', source: 'skill', type: 'skill', score: .5 }];
const data = { nodes, edges: [], focus: ['v1', 'c1', 's1'], reads: {} };
const iso = G.compute(Object.assign({ view: 'iso' }, data), 600, 500);
ok(Array.isArray(iso.discs) && iso.discs.length === 3, 'iso: one disc per family present (' + (iso.discs || []).length + ')');
const fams = (iso.discs || []).map((d) => d.name);
ok(fams.includes('vector') && fams.includes('cap') && fams.includes('skill'), 'the discs are the families of the nodes');
const d0 = (iso.discs || [])[0] || {};
ok(/^-?[\d.]+deg$/.test(d0.a0) && /^[\d.]+deg$/.test(d0.a1) && parseFloat(d0.a1) > 0, 'a wedge spans its sector (' + d0.a0 + ' → ' + d0.a1 + ')');
ok(/^[\d.]+%$/.test(d0.m0) && /^[\d.]+%$/.test(d0.m1) && parseFloat(d0.m1) > parseFloat(d0.m0) && parseFloat(d0.m0) > 0, 'a wedge is annular (the radial mask starts off the hub)');
ok(/^scale\(1\.103,0\.\d{3}\)$/.test(d0.tf) && d0.tf === 'scale(' + (iso.iso.kx * Math.SQRT2).toFixed(3) + ',' + (iso.iso.ky * Math.SQRT2).toFixed(3) + ')', 'the disc lies on the floor under the plate\'s projection — the tilt compute() chose for this column (' + d0.tf + ' · tilt ' + iso.iso.tilt.toFixed(1) + ')');
ok(iso.iso.tilt > 30 && iso.iso.tilt <= 58.3, 'a plot taller than it is wide steepens the tilt so the plate fills it');
const flat = G.compute(Object.assign({ view: 'iso' }, data), 900, 300);
ok(Math.abs(flat.iso.tilt - 30) < 1e-6 && /^scale\(1\.103,0\.552\)$/.test(flat.discs[0].tf), 'a wide, low plot keeps the 30° tilt — the plate\'s 1.103 × 0.552');
ok(typeof d0.col === 'string' && d0.col.length > 0 && d0.d > 0, 'a disc has its family colour and a diameter');
ok(iso.plate && typeof iso.plate.w === 'number', 'the plate stays');
// the plate's projected bounding box fits INSIDE the plot at every shape — the mirror's 660 × 661 plot with no lanes and
// no memory (where the old fit ran 977 px of plate past it), the grown column with the lanes and the memory ring, a wide
// low plot, the widget's mini box — with its clearance from the edge, the stems and the hub's pin inside the plot too
const memN = [{ id: 'sess', kind: 'session', label: 's', t: 1 }, { id: 'ms1', kind: 'message', label: 'm', t: 2 }, { id: 'f1', kind: 'fact', label: 'f', t: 3 }];
const inside = (o, W, H, lanes) => { const L = lanes ? 118 * o.k : 0, T = lanes ? 52 * o.k : 0; const p = o.plate;
  const box = p.x >= L && p.y >= T && p.x + p.w <= W && p.y + p.h <= H;
  const pins = (o.pins || []).every((q) => q.y >= T) && (o.stems || []).every((q) => q.y >= T && q.x >= L && q.x <= W);
  const hx = (W - L) / 2 - o.iso.pad, hy = (H - T) / 2 - o.iso.pad - o.iso.y0;            // the padded box the plate may fill (the stems' room above it)
  return { box, pins, fit: o.iso.fit, fill: Math.max(p.w / (2 * hx), p.h / (2 * hy)), w: Math.round(p.w), h: Math.round(p.h) }; };
[['the mirror\'s plot, no lanes, no memory', Object.assign({ view: 'iso' }, data), 660, 661, false],
 ['the grown column with the lanes and the memory ring', Object.assign({ view: 'iso', memory: memN, memEdges: [], loop: [{ id: 'l1', name: 'recon', status: 'ok' }, { id: 'l2', name: 'read', status: 'running' }], plan: [{ id: 'p1', label: 'find', status: 'done' }] }, data), 642, 681, true],
 ['a wide, low plot', Object.assign({ view: 'iso' }, data), 900, 300, false],
 ['a small square', Object.assign({ view: 'iso', memory: memN, memEdges: [] }, data), 400, 400, false]].forEach((c) => {
  const o = G.compute(c[1], c[2], c[3]); const r = inside(o, c[2], c[3], c[4]);
  ok(r.box && r.pins, 'iso fits ' + c[0] + ': plate ' + r.w + '×' + r.h + ' in ' + c[2] + '×' + c[3] + ', fit ' + r.fit.toFixed(2) + ', tilt ' + o.iso.tilt.toFixed(1) + '°');
  ok(r.fit <= 1 && r.fill >= 0.99, 'the plate spans the plot: the binding side fills its padded box' + (r.fill > 1.001 ? ' (past it: the 60·k radius floor holds this small plot, still inside)' : '') + ' — fill ' + r.fill.toFixed(3) + ', ' + Math.round(r.fit * 100) + '% of the plot'); });
const m = G.mini(Object.assign({ view: 'iso', memory: memN, memEdges: [] }, data), 262, 196); const rm = inside(m, 262, 196, false);
ok(rm.box, 'the mini\'s iso plate fits its box (' + rm.w + '×' + rm.h + ' in 262×196)');
const set = new Set((iso.discs || []).map((d) => d.a0)); ok(set.size === 3, 'the sectors differ family to family');
// the same discs are widest at the outer relevance radius: the diameter equals twice the outer radius
const galaxy = G.compute(Object.assign({ view: 'galaxy' }, data), 600, 500);
ok(Array.isArray(galaxy.discs) && galaxy.discs.length === 0, 'galaxy: no discs');
const flow = G.compute(Object.assign({ view: 'flow' }, data), 600, 500);
ok(Array.isArray(flow.discs) && flow.discs.length === 0, 'flow: no discs');
ok(/\.cg-disc\{/.test(src) && /conic-gradient\(from var\(--a0\)/.test(src) && /mask:radial-gradient/.test(src), 'the disc style: a conic wedge under a radial mask');
console.log(f ? f + ' failed' : 'all pass'); process.exit(f ? 1 : 0);
