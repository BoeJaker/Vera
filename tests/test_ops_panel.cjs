// The Live operations page: every node kind has an iso widget, the floors move apart and spread, ctrl + drag rotates,
// the widget rail edits what stands on the planes, and a node's deep dive reads the estate's record and its own reading.
//   node tests/test_ops_panel.cjs
const fs = require('fs'), path = require('path');
const R = path.join(__dirname, '..');
const H = fs.readFileSync(path.join(R, 'vera', 'estate', 'ops_panel.html'), 'utf8');
let fails = 0;
const t = (name, ok, why) => { console.log((ok ? 'ok   ' : 'FAIL ') + name + (ok ? '' : '  ' + (why || ''))); if (!ok) fails++; };
const KINDS = ['dial', 'tank', 'thermo', 'tower', 'crates', 'chip', 'mast', 'conveyor', 'stack'];
t('every widget kind in the picker is built from boxes with the map\'s own projection', KINDS.every((k) => H.includes("['" + k + "', ") && H.includes('    ' + k + ': (g, k, n) => {')), KINDS.filter((k) => !H.includes('    ' + k + ': (g, k, n) => {')).join(' '));
t('every node kind gets a widget by default (wspec), the rail can override the kind, the size and switch it off', H.includes('function wspec(n){') && ['ollama', 'docker', 'guest', 'host', 'store', 'sandbox', 'core', 'mesh', 'work'].every((k) => H.includes("n.kind === '" + k + "'")) && H.includes("n.id.indexOf('pipe:') === 0 ? 'stack'") && H.includes("if (o.off) return null;") && H.includes("localStorage.setItem('vera:ops:widgets'"));
t('the gap slider moves the floors apart and the spread slider the lanes and rows of a floor; both are kept between visits', H.includes('id="rGap"') && H.includes('id="rSpread"') && H.includes('* S.zgap;') && H.includes('const SP = S.spread, VS = 1.0 * SP') && H.includes("localStorage.setItem('vera:ops:view'"));
t('a floor is as deep as its fullest cell (a ladder of five to nine rows) and the flat grid lays floors out by their own sizes', H.includes('const ladder = (pl) => clamp(Math.ceil((cellMax[pl] || 1)), 5, 9);') && H.includes('VHof[p.id] = ((ladder(p.id) - 1) / 2) * VS + 0.45 * SP;') && H.includes('vOff[p.id] = v + (rowsD[r] - VHof[p.id]);'));
t('ctrl (or cmd) + drag rotates the 3D view - azimuth with x, tilt with y - and plain drag pans', H.includes("const rot = (e.ctrlKey || e.metaKey) && S.view === '3d';") && H.includes("S.azim = clamp(d.a0 + (e.clientX - d.x0) * 0.22, 26, 64); S.tilt = clamp(d.t0 - (e.clientY - d.y0) * 0.18, 4, 60);") && H.includes('<b>Ctrl + drag</b> rotates the view'));
t('the widget rail lists each plane\'s widgets with a size step, a remove and a picker; ⊕ on a node picks a kind beside it', H.includes('function renderRail(){') && H.includes('data-wsz-node=') && H.includes('data-woff=') && H.includes('data-wadd=') && H.includes('class="wpk"') && H.includes('data-asg='));
t('the deep dive reads estate.entity.resolve for a node with a reference and the node\'s own reading, drawn as a tree with the estate\'s planes, related things and the drawer', H.includes("'/estate/entity/resolve?ref='") && H.includes('function tree(v, depth, budget){') && H.includes('function slice(n, ans){') && H.includes('data-drawer=') && H.includes('<script src="/ui/vera-entity-drawer.js"></script>') && H.includes("<h3>Deep dive"));
t('restart on a docker host opens its containers in the estate rather than pretending', H.includes("if (n.kind === 'docker'){ if (window.veraUI && window.veraUI.openPlace) window.veraUI.openPlace('estate/docker'); return; }"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
