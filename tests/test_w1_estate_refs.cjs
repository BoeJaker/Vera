// Wave 1 (the plan: node cards take <kind>:<id> refs through the estate drawer): a desk table whose rows are estate things keeps
// each row's reference (VeraWidget.itemOf -> content.refs), and a row - in the table, list or cards view - opens the estate's
// entity drawer. A declaration may name its rows' reference from their fields (draw.ref: 'host:{id}').
'use strict';
const fs = require('fs'); const path = require('path'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const P = R('vera/widgets/present.js'), CV = R('vera/canvas/canvas_element.js');
const i = P.indexOf('  const rowRefOf=('), j = P.indexOf('  function itemOf(', i); const ref = new Function(P.slice(i, j) + '; return rowRefOf;')();
t('a row says what it is: its ref, a guest vmid, a host SSH login, a mesh node', ref({ ref: 'cert:x' }) === 'cert:x' && ref({ vmid: 145 }) === 'guest:145' && ref({ ssh_host_id: 'ab' }) === 'host:ab' && ref({ host_id: 'm1', kind: 'mesh' }) === 'mesh:m1');
t('or its declaration names it from its fields; a missing field is no reference', ref({ id: 'z9' }, 'host:{id}') === 'host:z9' && ref({ label: 'x' }, 'host:{id}') === '' && ref({ id: 1 }) === '' && ref(null) === '');
t('itemOf keeps the references only when some row has one (other tables unchanged)', P.includes('return refs.some(Boolean)?{ refs }:{};') && P.includes('const tpl=((W.presentationOf(cap)||{}).draw||{}).ref;'));
t('the table, list and cards views mark those rows; a click opens the entity drawer (loaded on first use)',
  CV.includes(`'<tr' + (refs[i] ? ' class="vc-tref" data-act="tblref" data-ref="'`) && CV.includes('vc-tv-r${refs[i] ? \' vc-tref" data-act="tblref"') &&
  CV.includes("if (act === 'tblref') {") && CV.includes("'/ui/vera-entity-drawer.js'") && CV.includes('window.veraEntityDrawer.open(ref)'));
const D = JSON.parse(R('tests/fixtures/presentation_declared.json'))['exec.ssh.hosts.list'] || {};
t('exec.ssh.hosts.list names its rows as SSH hosts', D.draw && D.draw.ref === 'host:{id}');
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);
