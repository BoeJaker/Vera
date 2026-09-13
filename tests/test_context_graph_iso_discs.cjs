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
const set = new Set((iso.discs || []).map((d) => d.a0)); ok(set.size === 3, 'the sectors differ family to family');
// the same discs are widest at the outer relevance radius: the diameter equals twice the outer radius
const galaxy = G.compute(Object.assign({ view: 'galaxy' }, data), 600, 500);
ok(Array.isArray(galaxy.discs) && galaxy.discs.length === 0, 'galaxy: no discs');
const flow = G.compute(Object.assign({ view: 'flow' }, data), 600, 500);
ok(Array.isArray(flow.discs) && flow.discs.length === 0, 'flow: no discs');
ok(/\.cg-disc\{/.test(src) && /conic-gradient\(from var\(--a0\)/.test(src) && /mask:radial-gradient/.test(src), 'the disc style: a conic wedge under a radial mask');
console.log(f ? f + ' failed' : 'all pass'); process.exit(f ? 1 : 0);
