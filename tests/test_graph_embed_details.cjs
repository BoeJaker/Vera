// WSU-88 (owner 2026-10-17: "i need to be able to click nodes to see details etc - vera graph js already has all of this its
// just not usable from the desk"): the embed's opt-in `details` lets vera_graph's own detail drawer open on a click; the
// desk turns it on; a double click is the card's action. vera_graph.js itself is unchanged.
//   node tests/test_graph_embed_details.cjs
'use strict';
const fs = require('fs'); const path = require('path'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const E = R('vera/graph_embed_element.js'), C = R('vera/canvas/canvas_element.js');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
t('with details a click opens the drawer (returns undefined) and is heard as vera-graph-detail', E.includes("if (self.hasAttribute('details')) {") && E.includes("new CustomEvent('vera-graph-detail'") && E.includes('return undefined;'));
t('without details nothing changes: the click is vera-graph-node and the drawer stays closed', E.includes('// Returning false leaves the detail drawer closed') && E.includes('return false;'));
t('with details a double click is the host action (vera-graph-node); without it vera_graph keeps its own', E.includes("onNodeDblClick: function (node) {") && E.includes("if (!self.hasAttribute('details')) return undefined;"));
t('the desk graph turns details on', C.includes("inner.setAttribute('details', '');"));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);