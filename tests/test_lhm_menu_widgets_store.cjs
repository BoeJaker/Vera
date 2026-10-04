// 2026-10-03 (owner), the LHM audit, points 3/4: "sub/internal LHMs editable, with widgets like the chat UI"; 2026-10-04:
// "go ahead on 3/4". A panel's menu takes widgets as the chat's menus do; the lists are kept on the server per panel and per
// menu (/ui/lhm/widgets), so the harness's docked menu, the panel opened on its own, another browser and an agent all see
// the same ones.
//   node tests/test_lhm_menu_widgets_store.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (...p) => fs.readFileSync(path.join(__dirname, '..', ...p), 'utf8');
const PY = R('vera', 'chat', 'chat_panels_capabilities.py'), LHM = R('vera', 'chat', 'vera-lhm.js'), VP = R('vera', 'vera-panel.js');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// the store
t('server: ui.lhm.widgets reads a panel\'s menus (by panel id or page path)', /"ui\.lhm\.widgets",\s*http_method="GET", http_path="\/ui\/lhm\/widgets"/.test(PY) && /async def cap_lhm_widgets\(panel: str = "", path: str = "", menu: str = ""/.test(PY));
t('server: ui.lhm.widgets.set replaces one menu\'s list, reset goes back to the panel\'s own', /"ui\.lhm\.widgets\.set",\s*http_method="POST", http_path="\/ui\/lhm\/widgets\/set"/.test(PY) && /await r\.hdel\(key, m\)/.test(PY) && /await r\.hset\(key, m, json\.dumps\(clean, default=str\)\)/.test(PY));
t('server: one hash per panel, a split view\'s copy is the same panel', /_LHM_W_KEY = "vera:ui:lhm:widgets:\{panel\}"/.test(PY) && /re\.sub\(r"--\\d\+\$", "", str\(panel or ""\)\.strip\(\)\)/.test(PY));
t('server: the harness\'s tab name for a docked panel (auto-<id>) is the same panel as the page on its own', /if p\.startswith\("auto-"\) and p not in UI_PANELS and p\[5:\] in UI_PANELS:/.test(PY));
t('server: a page path finds its panel from the registered frames', /for pid, spec in list\(UI_PANELS\.items\(\)\):/.test(PY) && /<iframe\[\^>\]\+src="\(\[\^"\?#\]\+\)/.test(PY));
t('server: bounded - 24 widgets a menu, 8 KB a record', /_LHM_W_MAX = 24/.test(PY) && /_LHM_W_REC_MAX = 8000/.test(PY));

// the docked menu (the harness draws it)
t('docked: a menu is named by what it holds (i:<item>, g:<group>), not its place in the rail', /function _absMenuKey\(spec, cur\)\{/.test(LHM) && /return t \? 'i:' \+ String\(t\.id\)\.slice\(0, 80\) : id;/.test(LHM) && /if\(m\[1\] === 'g'\) return 'g:' \+/.test(LHM));
t('docked: read from the server once per panel, drawn again from what came back', /function _absLoad\(key, skey, legacy, redraw, host\)\{/.test(LHM) && /fetch\(_base\(\) \+ '\/ui\/lhm\/widgets\?panel=' \+ encodeURIComponent\(key\)\)/.test(LHM) && /host && host\.isConnected && redraw\) redraw\(\);/.test(LHM));
t('docked: a list only this browser kept moves up to the server', /if\(l !== null\)\{ srv\[skey\] = l; _absPost\(key, skey, l\); \}/.test(LHM));
t('docked: a change is saved to the server (and cached here)', /function _absAddedSave\(key, skey, list\)\{[^\n]*_absPost\(key, skey, list\); \}/.test(LHM) && !/_absAddedSave\(key, cur\.id,/.test(LHM));

// the panel's own menu (standalone)
t('standalone: the panel\'s own menu shows the same widgets, under the open item\'s menu', /function initMenuWidgets\(host, btns, idOf, groupOf, items\) \{/.test(VP) && /initMenuWidgets\(strip \|\| host, btns, idOf, groupOf, items\);/.test(VP)
  && /return grouped \? 'g:' \+ \(groupOf\(b\) \|\| 'More'\)\.slice\(0, 80\) : 'i:' \+ String\(idOf\(b\)\)\.slice\(0, 80\);/.test(VP));
t('standalone: read by page path, saved with it', /fetch\('\/ui\/lhm\/widgets\?path=' \+ encodeURIComponent\(path\)\)/.test(VP) && /fetch\('\/ui\/lhm\/widgets\/set', \{ method: 'POST'/.test(VP));
t('standalone: ✎ - + Add widget from the chat\'s widget sheet, ✕ to take one out', /S\.open\(\{ mode: 'add', into: 'side', title: 'Add to this menu', templates: true, sizes: \['xs', 's', 'm'\] \}\)/.test(VP) && /rm\.title = 'Take it out of this menu';/.test(VP));
t('standalone: only in a menu that is a column, and out of the way when docked or folded', /var column = host\.id === 'sidebar' \|\| host\.tagName === 'ASIDE'/.test(VP) && /html\.vpb-nav-hosted \.vp-mw,body\.lhm-collapsed \.vp-mw,/.test(VP));
t('standalone: the widget element is loaded only when a widget is drawn or added', /sc\.src = '\/ui\/widgets\/widget_element\.js';/.test(VP));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
