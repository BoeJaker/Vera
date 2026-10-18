// 2026-10-03 (owner), the LHM audit, point 10: "menu options have descriptions and those descriptions are visible on
// mouseover and could even aide the agents" - "possibly a short and long, short mainly for ui use the long for agentic
// usage". Each panel listed here has been described item by item; add a panel when its menu is done.
//   node tests/test_lhm_menu_descriptions.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const DESCRIBED = {
  'Dream': ['vera', 'dream', 'dream_panel.html'],
  'Notebook': ['vera', 'research', 'notebook_panel.html'],
  'IDE': ['vera', 'ide', 'vscode_panel.html'],
  'Workspaces': ['vera', 'remote', 'workspace_panel.html'],
  'DAG workshop': ['vera', 'dag', 'dag_workshop_panel.html'],
  'Telegram': ['vera', 'telegram', 'telegram_panel.html'],
  'Fabric': ['vera', 'fabric', 'fabric_panel.html'],
  'Automations': ['vera', 'automations', 'automations_panel.html'],
  'Image Studio': ['vera', 'images', 'image_studio_panel.html'],
  'Comms': ['vera', 'accounts', 'comms_panel.html'],
  'Capabilities': ['vera', 'capabilities', 'cap_hub.html'],
  'Agents': ['vera', 'agents_skills_ontologies_panel.html'],
  'Estate / Models': ['vera', 'workers', 'workers_ollama_panel.html'],
  'Email': ['vera', 'email', 'email_panel.html'],
  'Net ops': ['vera', 'proxmox', 'netops_panel.html'],
  'UI Builder': ['vera', 'ui builder', 'ui_builder_panel.html'],
  'Markets': ['vera', 'markets', 'markets_studio_panel.html'],
};
for (const [name, file] of Object.entries(DESCRIBED)) {
  const H = fs.readFileSync(path.join(__dirname, '..', ...file), 'utf8');
  const btns = H.match(/<button\b[^>]*\bclass="[^"]*\bnav-btn\b[^"]*"[^>]*>/g) || [];   // whatever the attribute order
  const bad = btns.filter((b) => !(/data-label="[^"]+"/.test(b) && /data-desc="[^"]{8,140}"/.test(b) && /data-desc-long="[^"]{30,600}"/.test(b)));
  t(name + ': every one of its ' + btns.length + ' menu items has a name, a short and a long description', btns.length > 0 && bad.length === 0, bad.map((b) => b.slice(0, 80)).join(' | '));
  const icons = btns.map((b) => (b.match(/data-icon="([^"]*)"/) || [])[1]).filter((x) => x != null);
  t(name + ': no letter given as an icon', icons.every((i) => !/^[A-Za-z0-9]{1,2}$/.test(i)));
}

// menus whose items are not .nav-btn (view tabs, segment buttons, an icon rail): every item, by its id attribute
const BY_ID = [
  ['Research', ['vera', 'research', 'research_panel.html'], 'id', ['nv-r', 'nv-p', 'nv-s', 'nv-m', 'nv-lib', 'nv-nb', 'nv-pl', 'nv-nlp', 'nv-h', 'nv-bm', 'nv-cfg']],
  ['Calendar', ['vera', 'calendar', 'calendar_panel.html'], 'data-t', ['layers', 'events', 'agent', 'dump']],
  ['Calendar sections', ['vera', 'calendar', 'calendar_panel.html'], 'data-sec', ['calendar', 'sources', 'settings']],
  ['Perf', ['vera', 'monitor', 'perf_panel.html'], 'data-v', ['scan', 'stalls', 'logs']],
  ['Stack monitor', ['vera', 'monitor', 'system_monitor_panel.html'], 'id', ['smv-dash', 'smv-classic']],
  ['Home Assistant', ['vera', 'homeassistant', 'ha_panel.html'], 'data-t', ['control', 'entities', 'notify', 'config']],
  ['Vector browser', ['vera', 'vector browser', 'vector_browser_panel.html'], 'data-tab', ['overview', 'chroma', 'faiss', 'audit', 'compare', 'reencode']],
  ['ML lab', ['vera', 'machine learning', 'ml_lab_panel.html'], 'id', ['seg-build', 'seg-train', 'seg-agent']],
  ['Cap ontology', ['vera', 'ontologies', 'cap_ontology_panel.html'], 'id', ['vt-matrix', 'vt-graph', 'vt-list']],
  ['Storage (pxstore)', ['vera', 'proxmox', 'pxstore_panel.html'], 'data-p', ['overview', 'fs', 'docker', 'store', 'veradata', 'estate', 'setup']],
  ['Widgets', ['vera', 'widgets', 'widget_registry_panel.html'], 'id', ['wv-templates', 'wv-gallery', 'wv-sources']],
];
for (const [name, file, attr, ids] of BY_ID) {
  const H = fs.readFileSync(path.join(__dirname, '..', ...file), 'utf8');
  const bad = ids.filter((id) => {
    const tags = (H.match(new RegExp('<(?:button|div|a)\\b[^>]*\\s' + attr + '="' + id.replace(/[-]/g, '\\-') + '"[^>]*>', 'g')) || []);
    return !tags.some((tg) => /data-label="[^"]+"/.test(tg) && /data-desc="[^"]{8,140}"/.test(tg) && /data-desc-long="[^"]{12,600}"/.test(tg));
  });
  t(name + ': every one of its ' + ids.length + ' menu items has a name, a short and a long description', bad.length === 0, bad.join(', '));
}

// menus built in script: Business (a description map its _navBtn reads), Loop Lab (registerNav items), Markets' late buttons
{
  const B = fs.readFileSync(path.join(__dirname, '..', 'vera', 'business', 'business_panel.html'), 'utf8');
  const navIds = [...B.slice(B.indexOf('const NAV = ['), B.indexOf('const ALL_SECTIONS')).matchAll(/\['(\w+)','/g)].map((m) => m[1]);
  const storeIds = [...B.slice(B.indexOf('const STORE_SECTIONS=['), B.indexOf('function openStore')).matchAll(/\['(\w+)','/g)].map((m) => m[1]);
  const navDesc = B.slice(B.indexOf('const NAV_DESC = {'), B.indexOf('const STORE_DESC = {')), storeDesc = B.slice(B.indexOf('const STORE_DESC = {'), B.indexOf('function _navBtn('));
  t('Business: every section (' + navIds.length + ') and store section (' + storeIds.length + ') is described', navIds.length >= 13 && storeIds.length >= 10 && navIds.every((id) => new RegExp('\\b' + id + ":\\['(?:[^'\\\\]|\\\\.){8,}','(?:[^'\\\\]|\\\\.){12,}'\\]").test(navDesc)) && storeIds.concat('__back').every((id) => new RegExp('\\b' + id + ":\\['(?:[^'\\\\]|\\\\.){8,}','(?:[^'\\\\]|\\\\.){12,}'\\]").test(storeDesc)), navIds.concat(storeIds).join(','));
  t('Business: its menu buttons carry them', /const d=\(STORE_OPEN\?STORE_DESC:NAV_DESC\)\[id\]; b\.dataset\.label=label; if\(d\)\{ b\.dataset\.desc=d\[0\]; b\.dataset\.descLong=d\[1\]; \}/.test(B));
  const E = fs.readFileSync(path.join(__dirname, '..', 'vera', 'evolve', 'evolve_panel.html'), 'utf8');
  const items = (E.slice(E.indexOf('VeraPanelBridge.registerNav(['), E.indexOf(']);', E.indexOf('VeraPanelBridge.registerNav(['))).match(/\{id:'[^']+'[^}]*\}/g) || []);
  const railCount = (E.match(/<button class="btn[^"]*" data-sec="[a-z]+"/g) || []).length;
  t('Loop Lab: all ' + items.length + ' items carry an icon, a short and a long description', items.length === railCount && items.every((x) => /icon:'[^']+'/.test(x) && /desc:'[^']{8,}'/.test(x) && /desc_long:'[^']{8,}'/.test(x)));
  const M = fs.readFileSync(path.join(__dirname, '..', 'vera', 'markets', 'markets_studio_panel.html'), 'utf8');
  t('Markets: its late buttons (News, Flow, Dash) are described too', ['news', 'flow', 'dash'].every((v) => new RegExp("b\\.dataset\\.view='" + v + "';[^\\n]*b\\.dataset\\.desc='[^']{8,}'; b\\.dataset\\.descLong='[^']{20,}';").test(M)));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
