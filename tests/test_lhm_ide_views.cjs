// 2026-10-03 (owner), the LHM audit, point 7: a sub-UI (a top-bar option that replaces the page with other HTML or JS)
// belongs in the LHM; top bars are for settings and status. Probed on prod: the IDE's header carried five views - VS
// Code, Workbench, Remotes & Queue, Loop Lab, Dispatch, each loading another page - and the panel docked no menu.
//   node tests/test_lhm_ide_views.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'ide', 'vscode_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const header = H.slice(H.indexOf('<header>'), H.indexOf('</header>'));
t('the header no longer switches the view', header.length > 100 && !/data-view=/.test(header) && !/id="views"/.test(header));
t('the header keeps the instance, its status and actions', /id="inst-sel"/.test(header) && /id="inst-status"/.test(header) && /id="btn-connect"/.test(header) && /id="btn-refresh"/.test(header));

const nav = (H.match(/<nav id="views"[^>]*data-vera-lhm[\s\S]*?<\/nav>/) || [''])[0];
t('the views are the panel\'s menu (data-vera-lhm), a column beside the page', !!nav && /<div class="ide-body">\s*<!--[\s\S]*?-->\s*<nav id="views"/.test(H) && H.indexOf(nav) < H.indexOf('<main>'));
const btns = nav.match(/<button class="nav-btn[^"]*"[^>]*>/g) || [];
const views = ['vscode', 'classic', 'remotes', 'looplab', 'dispatch'];
t('all five views, each still a section of the page', btns.length === 5 && views.every((v) => btns.some((b) => b.includes('data-view="' + v + '"')) && H.includes('id="v-' + v + '"')));
t('each has a real icon (never a letter)', btns.every((b) => { const m = b.match(/data-icon="([^"]+)"/); return m && !/^[A-Za-z0-9]{1,2}$/.test(m[1]); }));
t('each has a name, a short and a long description', btns.every((b) => /data-label="[^"]+"/.test(b) && /data-desc="[^"]{8,}"/.test(b) && /data-desc-long="[^"]{30,}"/.test(b)));
t('VS Code is lit at load', /class="nav-btn active" data-view="vscode"/.test(nav));
t('the existing view code still drives it (same id, same buttons)', /\$\$\('#views button'\)\.forEach\(b=>b\.classList\.toggle\('active',b\.dataset\.view===v\)\);/.test(H) && /\$\$\('#views button'\)\.forEach\(b=>b\.onclick=\(\)=>showView\(b\.dataset\.view\)\);/.test(H));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
