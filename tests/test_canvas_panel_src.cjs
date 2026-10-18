// A panel on the canvas opens its own page (WSU-67, owner 2026-10-11: "ui panels opened on the desk seem to lock up and
// can't be interacted with"): an item without `src` drew /ui/panels/<id>, a route that 404s for every panel, so the
// frame was an empty page. The canvas resolves the address itself, the way the chat does.
//   node tests/test_canvas_panel_src.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = path.join(__dirname, '..');
const CE = fs.readFileSync(path.join(R, 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const a = CE.indexOf('  const PANEL_ALIASES = {'), b = CE.indexOf('  const BLOCK = {');
t('the resolver sits before the renderers', a > 0 && b > a);
const src = CE.slice(a, b);
const run = async (reg, live) => {      /* reg: the /ui/panels answer; live: paths a HEAD answers 200 for */
  const calls = []; const ctx = { root: { _veraBase: '' }, fetch: async (url, o) => { calls.push((o && o.method) || 'GET'); if (url === '/ui/panels') return { json: async () => reg }; return { ok: live.includes(url) }; } };
  vm.runInNewContext(src + '\nthis.panelSrc = panelSrc; this.PANEL_SRC = PANEL_SRC;', ctx); return { ctx, calls };
};
(async () => {
  let { ctx } = await run([{ id: 'system-monitor', html: '<div><iframe src="/sysmon/panel" style="x"></iframe></div>' }], []);
  t('1. the iframe src in the registered html', await ctx.panelSrc('system-monitor') === '/sysmon/panel');
  t('   cached per panel', ctx.PANEL_SRC['system-monitor'] === '/sysmon/panel');
  ({ ctx } = await run([{ id: 'memory-graph', html: '<div id="m"></div>' }], []));
  t('2. the alias table (inline panels with a standalone page)', await ctx.panelSrc('memory-graph') === '/ui/panels/file/memory_graph_panel.html');
  ({ ctx } = await run([{ id: 'odd-one', html: '<div></div>' }], ['/ui/panels/odd-one']));
  t('3. a dedicated /ui/panels/<id> route when it answers', await ctx.panelSrc('odd-one') === '/ui/panels/odd-one');
  ({ ctx } = await run([{ id: 'dag-workshop', html: '<div></div>' }], ['/ui/panels/file/dag_workshop_panel.html']));
  t('4. a file-name guess when it answers', await ctx.panelSrc('dag-workshop') === '/ui/panels/file/dag_workshop_panel.html');
  ({ ctx } = await run([{ id: 'nothing', html: '<div></div>' }], []));
  t('5. nothing answers: empty, never the dead /ui/panels/<id> guess', await ctx.panelSrc('nothing') === '');
  ({ ctx } = await run([{ id: 'cal', label: 'Calendar', html: '<iframe src="/cal/panel">' }], []));
  t('   a panel named by its label resolves too', await ctx.panelSrc('Calendar') === '/cal/panel');
  t('the panel item no longer draws /ui/panels/<id> as its frame', !/c\.src \|\| \(id \? '\/ui\/panels\/' \+ encodeURIComponent\(id\)/.test(CE));
  t('while it resolves the item says so; with nothing to open it says that (no blank frame)', CE.includes('`<div class="vc-dim vc-pwait">opening ${esc(c.title || id)}…</div>`') && CE.includes('has no page that can open here.'));
  t('the element asks once per panel and draws again when the address comes back', /_panelResolve\(id\) \{[\s\S]{0,200}P\.has\(id\)[\s\S]{0,200}panelSrc\(id\)\.then\(\(\) => \{ P\.delete\(id\); this\._html = null; if \(this\._doc\) this\.render\(this\._doc\);/.test(CE));
  console.log(fails ? fails + ' failed' : 'all passed'); process.exit(fails ? 1 : 0);
})();
