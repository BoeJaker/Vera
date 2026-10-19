// Wave 1 (panel-derived): the Proxmox panel's render() (node cards, core temps, guest cards, storage) moved once into
// <vera-proxmox-status> (vera/proxmox/proxmox_status_element.js). The panel draws through it with `actions` (its guest power and
// console buttons, calling its own guest() / openConsole()); the desk mounts it for a proxmox.status answer, seeded, display only.
// tests/fixtures/proxmox_status_parity.json holds what the panel's own code drew before.
'use strict';
const fs = require('fs'); const path = require('path'); const vm = require('vm'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const E = R('vera/proxmox/proxmox_status_element.js'), P = R('vera/proxmox/proxmox_panel.html'), WE = R('vera/widgets/widget_element.js'), PC = R('vera/proxmox/proxmox_capabilities.py');
const FX = JSON.parse(R('tests/fixtures/proxmox_status_parity.json'));
const estate = { chip: (r, x) => '<span data-entity="' + r + '">' + x + '</span>', refFor: (q) => 'host:' + q.node };
const load = (win) => { const ctx = { window: win, HTMLElement: class {}, customElements: { get: () => false, define() {} }, Object, String, Math, Set, JSON }; vm.createContext(ctx); vm.runInContext(E, ctx); return ctx.window.VeraProxmoxStatus; };
const M = load({ veraEstate: estate });
t('with actions it draws what the panel drew, byte for byte (every case)', FX.cases.every((c, i) => M.renderHtml(c.status, c.temps, { actions: true }) === FX.html[i]));
const noButtons = (h) => h.replace(/<div class="guest-actions">[\s\S]*?<\/div>\n      <div class="guest-actions">[\s\S]*?<\/div>/g, '');
t('without actions (the desk) it draws the same cards with no power or console buttons', FX.cases.every((c, i) => { const h = M.renderHtml(c.status, c.temps, {}); return h === noButtons(FX.html[i]) && !h.includes('<button'); }));
const bare = load({}).renderHtml(FX.cases[0].status, FX.cases[0].temps, {});
t('before vera-estate.js loads: node names plain, guests still open the entity drawer', !bare.includes('data-entity="host:') && bare.includes('data-entity="guest:104"') && bare.includes('🖥 pve1</span>'));
t('the panel draws through the element and keeps its controls; its own render copy is gone',
  P.includes("el=document.createElement('vera-proxmox-status'); el.setAttribute('refresh','0'); el.setAttribute('actions','');") && P.includes('el.seed({status:SNAP, temps:TEMPS});')
  && P.includes('<script src="/ui/elements/proxmox_status.js"></script>') && !P.includes('function metric(') && !P.includes("SNAP.guests.filter(g=>!g.template)")
  && P.includes('async function guest(node,type,vmid,action)') && P.includes('async function openConsole('));
t('the element is served and the widget element form knows it', PC.includes('@APP.get("/ui/elements/proxmox_status.js", include_in_schema=False)') && WE.includes("'vera-proxmox-status': 'proxmox_status'"));
const decl = JSON.parse(R('tests/fixtures/presentation_declared.json'))['proxmox.status'] || {};
t('proxmox.status opens as the element (the guest table kept as its view, guests as estate refs)', decl.form === 'element' && decl.draw && decl.draw.tag === 'vera-proxmox-status'
  && decl.map && decl.map.rows === 'guests' && decl.draw.ref === 'guest:{vmid}' && decl.draw.attrs && decl.draw.attrs.cluster === '{cluster_id}');
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);
