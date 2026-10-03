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
};
for (const [name, file] of Object.entries(DESCRIBED)) {
  const H = fs.readFileSync(path.join(__dirname, '..', ...file), 'utf8');
  const btns = H.match(/<button class="nav-btn[^"]*"[^>]*>/g) || [];
  const bad = btns.filter((b) => !(/data-label="[^"]+"/.test(b) && /data-desc="[^"]{8,140}"/.test(b) && /data-desc-long="[^"]{30,600}"/.test(b)));
  t(name + ': every one of its ' + btns.length + ' menu items has a name, a short and a long description', btns.length > 0 && bad.length === 0, bad.map((b) => b.slice(0, 80)).join(' | '));
  const icons = btns.map((b) => (b.match(/data-icon="([^"]*)"/) || [])[1]).filter((x) => x != null);
  t(name + ': no letter given as an icon', icons.every((i) => !/^[A-Za-z0-9]{1,2}$/.test(i)));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
