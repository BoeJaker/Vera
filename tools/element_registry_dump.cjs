// The element registry, read-only, as JSON (DO-0.2): every entry VeraItem.registerBody holds once the canvas's body kinds have
// registered beside the shared elements - for the coordinator and the later waves. It reads the registry; it holds no table.
//   node tools/element_registry_dump.cjs [--role body|element] [--surface desk|dashboard|chat|lhm|panel|panel:<id>] [--contract <kind>]
'use strict';
const path = require('path');
const ROOT = path.join(__dirname, '..');
const VI = require(path.join(ROOT, 'vera', 'ui', 'vera_item.js'));   // VeraItem: the registry and the shared elements
require(path.join(ROOT, 'vera', 'canvas', 'canvas_element.js'));      // ...and the canvas's body kinds register with it

const arg = (name) => { const i = process.argv.indexOf('--' + name); return i > 0 ? process.argv[i + 1] : undefined; };
const filter = {}; ['role', 'surface', 'contract', 'face', 'element'].forEach((k) => { if (arg(k)) filter[k] = arg(k); });
const entries = VI.elements(filter).map((e) => { const o = {}; Object.keys(e).forEach((k) => { if (typeof e[k] !== 'function' && k !== 'draw' && k !== 'natural') o[k] = e[k]; }); return o; });
const out = {
  about: 'VeraItem.registerBody is the one element registry (DO-0.2): role body = an item kind the container hosts; role element = a shared element. '
    + 'views / fit / minSize / actions are what the head and the menu read; the rest is what it draws and where it goes.',
  filter, count: entries.length,
  vocabulary: { faces: VI.FACES, sizes: VI.SIZES, spans: VI.SPANS, surfaces: VI.SURFACES.concat(['panel:<id>']), roles: VI.ROLES, contract_by: VI.CONTRACT_BY, navigation_to: VI.NAV_TO },
  by_role: VI.ROLES.reduce((o, r) => { o[r] = entries.filter((e) => e.role === r).map((e) => e.kind); return o; }, {}),
  entries,
};
process.stdout.write(JSON.stringify(out, null, 1) + '\n');
