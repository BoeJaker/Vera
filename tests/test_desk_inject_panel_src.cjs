// WSU-104: an "inject" panel (registered html + js, no page of its own) opens on the desk through the server's standalone
// panel page /ui/panel/window?id=, not a guessed <name>_panel.html that answers "not found". Runs the real panelSrc.
'use strict';
const fs = require('fs'); const path = require('path'); const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const i = src.indexOf('  async function panelSrc(id) {'); assert(i > 0, 'panelSrc found');
const j = src.indexOf('\n  }\n', i) + 4; const body = src.slice(i, j);
const reg = [{ id: 'cap-list', mode: 'inject', html: '<vera-cap-list></vera-cap-list>' }, { id: 'calendar-panel', mode: 'inject', html: '<iframe src="/cal/panel"></iframe>' }, { id: 'ops', mode: 'tab', html: '' }];
const seen = [];
const fetch = async (url, opts) => { seen.push(url); if (url.endsWith('/ui/panels')) return { json: async () => reg }; return { ok: /ops_panel\.html$/.test(url) }; };
const make = new Function('root', 'fetch', 'PANEL_SRC', 'PANEL_ALIASES', 'let PANEL_REG = null;\n' + body + '\nreturn panelSrc;');
(async () => {
  const panelSrc = make({ _veraBase: '' }, fetch, {}, {});
  assert.strictEqual(await panelSrc('cap-list'), '/ui/panel/window?id=cap-list'); console.log('ok   an inject panel with no page -> the standalone window page');
  assert.strictEqual(await panelSrc('calendar-panel'), '/cal/panel'); console.log('ok   an inject panel that frames its own page keeps that page');
  assert.strictEqual(await panelSrc('ops'), '/ui/panels/file/ops_panel.html'); console.log('ok   a tab panel still finds its file');
  console.log('test_desk_inject_panel_src: 3 passed');
})().catch((e) => { console.error(e); process.exit(1); });
