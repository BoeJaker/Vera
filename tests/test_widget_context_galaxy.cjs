// The mini context graph as a widget form: the Canvas board's galaxy (rings · records by family · labels · hub · edges),
// its iso / flow / timeline views, All edges, a family switched off, the lane drawing kept, XL the full element.
const fs = require('fs'); const path = require('path'); const vm = require('vm');
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
const win = { customElements: { get: () => undefined, define: () => {} }, addEventListener: () => {}, document: { createElement: () => ({ style: {}, setAttribute: () => {}, appendChild: () => {} }), head: { appendChild: () => {} }, querySelectorAll: () => [] }, HTMLElement: class {}, location: { origin: '' } };
win.window = win; vm.createContext(win);
try { vm.runInContext(src, win); } catch (e) { console.log('load: ' + e.message); }
const VW = win.VeraWidget; if (!VW) { console.log('FAIL no VeraWidget'); process.exit(1); }
let fails = 0; const ok = (c, m) => { console.log((c ? 'ok   ' : 'FAIL ') + m); if (!c) fails++; };
const data = { nodes: [
  { id: 'v1', label: 'fabric_capabilities.py', source: 'vector', score: 0.94 }, { id: 'v2', label: 'state_paths.py', source: 'vector', score: 0.6, included: false },
  { id: 'g1', label: 'commit 312caef', source: 'graph', score: 0.9 }, { id: 'm1', label: 'boot memo', source: 'memory', score: 0.8 }, { id: 'c1', label: 'obs.provenance', source: 'cap', score: 0.75 },
  { id: 'f1', label: 'fabric.digest', source: 'fabric', score: 0.85 }, { id: 's1', label: 'fabric-ops', source: 'skill', score: 0.8 } ],
  rels: [{ from: 'v1', to: 'g1', label: 'CITES' }, { from: 'm1', to: 'v1', label: 'RELATED' }] };
const gal = VW.draw('context_graph', data, 'm', { height: 196, bare: true });
ok(/class="vw-gal vw-gal-galaxy"/.test(gal), 'the galaxy view by default');
ok((gal.match(/vw-gring/g) || []).length === 3, 'three rings');
ok((gal.match(/class="vw-gd/g) || []).length === 7, 'every record a dot');
ok(/vw-gd hollow/.test(gal) && /vw-gd lit/.test(gal), 'a related-not-injected record is hollow, a relevant one lit');
ok((gal.match(/vw-glb/g) || []).length === 6, 'one label per family');
ok(/vw-ghub[^>]*>aide<b>7 rec<\/b>/.test(gal), 'the hub names the aide and the record count');
ok((gal.match(/vw-gedge/g) || []).length === 2, 'the cited edges');
ok(/data-id="v1"/.test(gal), 'a dot carries its record id for the host to open');
const all = VW.draw('context_graph', data, 'm', { height: 196, bare: true, allEdges: true });
ok((all.match(/vw-gedge faint/g) || []).length === 7, 'All edges: every record to the hub, faint');
const iso = VW.draw('context_graph', data, 'm', { height: 196, bare: true, view: 'iso' });
ok(/vw-gal-iso/.test(iso) && /vw-gstem/.test(iso), 'iso: the plane and the stems');
ok(/vw-gal-flow/.test(VW.draw('context_graph', data, 'm', { bare: true, view: 'flow' })) && /vw-gal-timeline/.test(VW.draw('context_graph', data, 'm', { bare: true, view: 'timeline' })), 'flow and timeline views');
const off = VW.draw('context_graph', data, 'm', { height: 196, bare: true, off: { vector: true } });
ok(/opacity:0\.12/.test(off), 'a family switched off is dimmed');
const col = VW.draw('context_graph', data, 'm', { height: 196, bare: true, color: (f) => f === 'vector' ? '#123456' : '' });
ok(/--c:#123456/.test(col), 'the host may colour a family');
ok(/class="vw-cg"/.test(VW.draw('context_graph', data, 'm', { height: 70, bare: true, layout: 'lanes' })), 'the lane drawing stays behind layout:lanes');
ok(/vw-cgfull/.test(VW.draw('context_graph', data, 'xl', {})), 'XL is the full context graph element');
ok(/wempty/.test(VW.draw('context_graph', { nodes: [] }, 'm', { bare: true })), 'no records: the empty word');
ok(/\.vw-gd\{/.test(VW.css()) && /\.vw-ghub\{/.test(VW.css()), 'the galaxy has its rules in the widget css');
console.log(fails ? fails + ' failed' : 'all pass'); process.exit(fails ? 1 : 0);
