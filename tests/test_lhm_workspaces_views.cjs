// 2026-10-03 (owner), the LHM audit, point 7: a sub-UI (a top-bar option that replaces the page with other HTML)
// belongs in the LHM; top bars keep settings, status and actions. Probed on prod: the Workspaces header switched
// between the workspace grid and the integrations hub (another page in a frame), and the panel docked no menu.
//   node tests/test_lhm_workspaces_views.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'remote', 'workspace_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const main = H.slice(H.indexOf('<div id="main">'), H.indexOf('<iframe id="intFrame"'));
t('the header no longer switches the view', main.length > 50 && !/setView\(/.test(main) && !/class="seg"/.test(main));
t('the header keeps its actions (Apps, Edit, + Widget, Save) and the title', /openApps\(\)/.test(main) && /id="editBtn"/.test(main) && /id="addBtn"/.test(main) && /id="saveBtn"/.test(main) && /id="wsTitle"/.test(main));

const nav = (H.match(/<nav id="wsViews"[^>]*data-vera-lhm[\s\S]*?<\/nav>/) || [''])[0];
t('the views are the panel\'s menu, a column of its own (outside #side, which Integrations mode hides)', !!nav && H.indexOf(nav) < H.indexOf('<div id="side">') && /body\.intmode #side/.test(H) && !/body\.intmode[^{]*#wsViews/.test(H));
const btns = nav.match(/<button class="nav-btn[^"]*"[^>]*>/g) || [];
t('Workspaces and Integrations, by the ids setView lights', btns.length === 2 && /id="segWs"[^>]*onclick="setView\('ws'\)"/.test(nav) && /id="segInt"[^>]*onclick="setView\('int'\)"/.test(nav));
t('each has a real icon, a name, a short and a long description', btns.every((b) => { const m = b.match(/data-icon="([^"]+)"/); return m && !/^[A-Za-z0-9]{1,2}$/.test(m[1]) && /data-label="[^"]+"/.test(b) && /data-desc="[^"]{8,}"/.test(b) && /data-desc-long="[^"]{30,}"/.test(b); }));
t('Workspaces is lit at load, and setView still lights by id', /class="nav-btn on" id="segWs"/.test(nav) && /const a=document\.querySelector\('#segWs'\), b=document\.querySelector\('#segInt'\);/.test(H));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
