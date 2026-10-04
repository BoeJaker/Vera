// 2026-10-04 (owner): "fix the ml lab hard coded theme it should be like the other pages and accept the vera themes.
// fully integrate the research lhm. declare primary actions for evey panel its appropriate. can you make a set of base
// template for ui elements - a bootstrap for widgets and ui panels, graphs maybe too - document it in the documentation
// and in the vera improvement skill. possibly worth defining a vera design skill and reefer to it in the improvement
// skill - register it in the vera external skills registry."
//   node tests/test_ui_bootstrap.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const P = (...p) => path.join(__dirname, '..', ...p);
const R = (...p) => fs.readFileSync(P(...p), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const inlineOk = (h) => { const re = /<script(?![^>]*\bsrc=)([^>]*)>([\s\S]*?)<\/script>/gi; let m, bad = 0; while ((m = re.exec(h))) { try { new vm.Script(m[2]); } catch (e) { bad++; } } return bad === 0; };
/* a colour literal outside a fallback position (", #hex" / ", rgba(" inside var() or color-mix) */
const bareColours = (css) => (css.match(/(^|[^,\s])\s*(#[0-9a-fA-F]{3,8}\b|rgba?\()/gm) || []).filter(x => !/^[,]/.test(x));

// the bootstrap
const CSS = R('vera', 'ui', 'bootstrap.css');
t('bootstrap.css: the page parts (grid, top bar, sections, cards, buttons, badges, key/value, tables)', ['.vb-root', '.vb-topbar', '.vb-section', '.card {', '.btn {', '.btn.primary', '.badge {', '.kv {', 'table.vb-table'].every(s => CSS.includes(s)));
t('bootstrap.css: every colour is a theme name with a fallback - no bare literal', bareColours(CSS.replace(/\/\*[\s\S]*?\*\//g, '')).length === 0, JSON.stringify(bareColours(CSS.replace(/\/\*[\s\S]*?\*\//g, '')).slice(0, 4)));
const LIBS = R('vera', 'ui', 'libs.py');
t('served: /ui/bootstrap.css and /ui/templates/<name> (only the named files)', /@APP\.get\("\/ui\/bootstrap\.css"/.test(LIBS) && /@APP\.get\("\/ui\/templates\/\{name\}"/.test(LIBS) && /ct = TEMPLATES\.get\(name\)/.test(LIBS));
const CPC = R('vera', 'chat', 'chat_panels_capabilities.py');
t('ui.templates: lists the starting files for an agent, hands one over by name', /"ui\.templates",\s*http_method="GET", http_path="\/ui\/templates"/.test(CPC) && /async def cap_ui_templates\(name: str = ""/.test(CPC));
for (const n of ['panel.html', 'widget.json', 'widget.html', 'graph.html']) {
  t('template ' + n + ' exists and is named in both lists', fs.existsSync(P('vera', 'ui', 'templates', n)) && LIBS.includes('"' + n + '"') && CPC.includes('"' + n + '"'));
}
const PANEL = R('vera', 'ui', 'templates', 'panel.html');
t('panel template: the canonical menu, described items, a settings item, a primary action, per-item widgets', /<aside id="sidebar" data-vera-lhm>/.test(PANEL) && /class="nav-btn active" data-section="overview" data-label="Overview" data-desc=/.test(PANEL)
  && /data-desc-long=/.test(PANEL) && /data-vera-settings="#sec-settings"/.test(PANEL) && /data-lhm-cta=""/.test(PANEL) && /data-lhm-widgets=/.test(PANEL) && /class="nav-grp" data-icon=/.test(PANEL));
t('panel template: the shared scripts in order, own work after them', /vera-ui\.js"><\/script>\s*<script src="\/ui\/vera-panel\.js"><\/script>\s*<script src="\/ui\/vera-panel-bridge\.js"><\/script>\s*<script src="\/ui\/vera-settings\.js"><\/script>/.test(PANEL) && /document\.addEventListener\('DOMContentLoaded', refresh\);/.test(PANEL));
t('panel template: its own rules use the theme only', bareColours((PANEL.match(/<style>([\s\S]*?)<\/style>/) || [, ''])[1].replace(/\/\*[\s\S]*?\*\//g, '')).length === 0);
for (const n of ['panel.html', 'widget.html', 'graph.html']) t('template ' + n + ': inline scripts parse', inlineOk(R('vera', 'ui', 'templates', n)));
const WJ = JSON.parse(R('vera', 'ui', 'templates', 'widget.json'));
t('widget template: the registry\'s record shape', ['id', 'name', 'form', 'reads', 'draw', 'can', 'placed', 'source'].every(k => k in WJ) && WJ.reads.cap && WJ.draw.form === WJ.form);
const GR = R('vera', 'ui', 'templates', 'graph.html');
t('graph template: the one renderer, both forms; edges are {from, to, rel}', /veraUI\.Graph\.create\(/.test(GR) && /<vera-graph-embed /.test(GR) && /\{ from: 'a', to: 'b', rel: /.test(GR) && /\/ui\/vera-graph-embed\.js/.test(GR));
const UI = R('vera', 'vera-ui.js');
t('veraUI.color: a theme colour for a canvas, cached until the theme changes', /function themeColor\(name, alpha\)\{/.test(UI) && /color: themeColor,/.test(UI) && /attributeFilter: \['style', 'data-theme', 'data-style', 'class'\] \}\); \}catch\(e\)\{\}/.test(UI));

// the documentation, the skill, the registry
const DOC = R('documentation', '51-ui-design-system.md');
t('documented: doc 51 - layers, templates, colour tokens, the menu contract, appearance, settings, widgets, graphs, checks', ['## The layers', '## Starting something: the templates', '## Colour', '## The menu', '## Appearance on a panel', '## Settings', '## Widgets', '## Graphs', '## Checks'].every(h => DOC.includes(h)));
const SK = R('.claude', 'skills', 'vera-design', 'SKILL.md');
t('the vera-design skill: frontmatter, the bootstrap, the rules, live verification', /^---\nname: vera-design\ndescription: /.test(SK) && /## 1\. Start from the bootstrap/.test(SK) && /## 3\. Verify live/.test(SK));
for (const s of ['improve-vera-sandboxed', 'improve-vera']) t(s + ' refers to the vera-design skill', /\*\*Any UI change[^*]*also follows the `vera-design`/.test(R('.claude', 'skills', s, 'SKILL.md')));
t('registered: the registry seeds vera-design with its full content', /"kind": "skill", "name": "vera-design",/.test(R('vera', 'registry', 'registry_capabilities.py')) && /"source": \{"origin": "claude-code", "path": "\.claude\/skills\/vera-design\/SKILL\.md"\}/.test(R('vera', 'registry', 'registry_capabilities.py')));

// ML Lab follows the theme
for (const f of ['ml_lab_panel.html', 'ml_training_panel.html', 'ml_workshop_panel.html']) {
  const H = R('vera', 'machine learning', f);
  const root = (H.match(/:root\{[^}]*\}/) || [''])[0], rest = H.replace(root, '');
  t('ML Lab ' + f + ': its palette names the theme\'s (--void, --teal, --amber, --rose, --violet, --lime)', /--void:var\(--bg0,#050709\)/.test(root) && /--teal:var\(--acc2,#00d4aa\)/.test(root) && /--rose:var\(--err,var\(--red,#e05a6b\)\)/.test(root));
  t('ML Lab ' + f + ': no literal copy of its palette outside it; canvases ask __tc()', !/rgba\(56,\s*120,\s*220|rgba\(0,\s*212,\s*170|#3878dc|#00d4aa/.test(rest) && /window\.__tc = window\.__tc \|\|/.test(H) && inlineOk(H));
}

// Research's menu
const RS = R('vera', 'research', 'research_panel.html');
t('Research: the menu column (names beside icons, the shared width), on the shared menu script', /grid-template-columns:var\(--lhm-width,184px\) 1fr/.test(RS) && (RS.match(/<span class="nl">/g) || []).length >= 11 && /<script src="\/ui\/vera-panel\.js"><\/script>/.test(RS));
t('Research: folds to its icons, steps aside when docked', /body\.lhm-collapsed #shell\{grid-template-columns:48px 1fr\}/.test(RS) && /html\.vpb-nav-hosted #shell\{grid-template-columns:0 1fr\}html\.vpb-nav-hosted #nav\{display:none\}/.test(RS));
const VP = R('vera', 'vera-panel.js');
t('a menu\'s direction is read from where its items sit (Research\'s column had read as a strip)', /function isColumn\(host, btns\) \{/.test(VP) && /return Math\.abs\(b\.top - a\.top\) > Math\.abs\(b\.left - a\.left\);/.test(VP));

// primary actions
const CTAS = [['dag', 'dag_workshop_panel.html', 'New DAG'], ['ide', 'vscode_panel.html', 'New worker'], ['ide', 'ide_remote_panel.html', 'Run a task'], ['ide', 'ide_inspect_panel.html', 'New snapshot'],
  ['web', 'web_api_panel.html', 'Add provider'], ['remote', 'workspace_panel.html', 'New workspace'], ['research', 'notebook_panel.html', 'New notebook'], ['accounts', 'accounts_panel.html', 'Add account'],
  ['mcp', 'mcp_catalog_panel.html', 'Add server'], ['widgets', 'widget_registry_panel.html', 'New widget'], ['operator', 'operator_studio_panel.html', 'Run to goal'], ['integrations', 'integrations_panel.html', 'Add integration']];
for (const [d, f, label] of CTAS) { const H = R('vera', d, f); t(f + ': names its primary action "' + label + '" (once)', (H.match(/data-lhm-cta=/g) || []).length === 1 && H.includes('data-lhm-cta="" data-label="' + label + '"')); }
const BR = R('vera', 'chat', 'vera-panel-bridge.js'), HN = R('vera', 'capability_orchestration.html');
t('a menu-less panel\'s primary action reaches the harness (state.cta) and ends its list', /try\{ var _ct = _navCtas\(\); if\(_ct\.length\) st\.cta = _ct; \}catch\(e\)\{\}/.test(BR)
  && /const _panelCtaCache = \{\};/.test(HN) && /if\(JSON\.stringify\(nav\) === prevSig && !ctaChanged\) return;/.test(HN) && /_lhmNavSelect\(act\.pid, 'cta:' \+ c\.i\);/.test(HN));

t('a NESTED menu-less panel\'s primary action ends its host\'s open menu; a press goes down to it', /k\.cta = \(d\.state && Array\.isArray\(d\.state\.cta\)\)/.test(BR) && /function _shownKidCta\(\)\{/.test(BR) && /if\(m\.id === act\.menu && !\(c && c\.for\)\)\{ var kc = _shownKidCta\(\); if\(kc\) m\.cta = \{ id: 'c:cta:' \+ kc\.cta\[0\]\.i/.test(BR) && /if\(id\.indexOf\('c:cta:'\) === 0\)\{/.test(BR));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
