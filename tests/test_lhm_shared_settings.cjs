// 2026-10-03 (owner), the LHM audit, point 5: "the settings menu in the chat ui ... that settings menu is the cannon
// settings menu style for all panels". vera-settings.js draws the chat's Settings page for any panel, from the panel's own
// settings section, moving its blocks in (never copying) and back on close. Live-checked on Telegram, Email and Storage.
//   node tests/test_lhm_shared_settings.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (...p) => fs.readFileSync(path.join(__dirname, '..', ...p), 'utf8');
const VS = R('vera', 'chat', 'vera-settings.js'), CHAT = R('vera', 'chat', 'chat_panel.html'), PY = R('vera', 'chat', 'chat_panels_capabilities.py');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// the chat's page is the canon: the shared one keeps its measures
const pairs = [
  ['a page over everything', /#settingsPage\{position:fixed;top:0;right:0;bottom:0;left:0;z-index:5200;display:flex;flex-direction:column;/, /\.vs-page\{position:fixed;top:0;right:0;bottom:0;left:0;z-index:5200;display:flex;flex-direction:column;/],
  ['a 52px header', /#settingsPage \.sp-hd\{display:flex;align-items:center;gap:14px;height:52px;padding:0 22px;/, /\.vs-page \.vs-hd\{display:flex;align-items:center;gap:14px;height:52px;padding:0 22px;/],
  ['a 19px title', /#settingsPage \.sp-hd h1\{margin:0;font-size:19px;font-weight:600;letter-spacing:-\.01em\}/, /\.vs-page \.vs-hd h1\{margin:0;font-size:19px;font-weight:600;letter-spacing:-\.01em\}/],
  ['a 232px index beside the content', /#settingsPage \.sp-bd\{flex:1;min-height:0;display:grid;grid-template-columns:232px 1fr;gap:14px;padding:14px 22px 18px\}/, /\.vs-page \.vs-bd\{flex:1;min-height:0;display:grid;grid-template-columns:232px 1fr;gap:14px;padding:14px 22px 18px\}/],
  ['index rows 28px, 11.5px', /#settingsPage \.sp-irow\{display:flex;align-items:center;gap:8px;height:28px;padding:0 8px;border-radius:var\(--r-sm,6px\);font-size:11\.5px;/, /\.vs-page \.vs-irow\{display:flex;align-items:center;gap:8px;height:28px;padding:0 8px;border-radius:var\(--r-sm,6px\);font-size:11\.5px;/],
  ['cards in a 340px grid', /#settingsPage \.sp-grid\{display:grid;grid-template-columns:repeat\(auto-fill,minmax\(340px,1fr\)\);gap:12px;/, /\.vs-page \.vs-grid\{display:grid;grid-template-columns:repeat\(auto-fill,minmax\(340px,1fr\)\);gap:12px;/],
  ['cards padded 10px 12px 12px', /#settingsPage \.sp-card\{[^}]*padding:10px 12px 12px;display:flex;flex-direction:column;gap:6px;min-width:0;font-size:11\.5px;/, /\.vs-page \.vs-card\{[^\n]*?padding:10px 12px 12px;display:flex;flex-direction:column;gap:6px;min-width:0;font-size:11\.5px;/],
];
for (const [name, chat, shared] of pairs) t(name + ' - as the chat\'s page', chat.test(CHAT) && shared.test(VS), 'chat ' + chat.test(CHAT) + ' shared ' + shared.test(VS));

// behaviour
t('the item opens it before the panel\'s own handler (capture on the document)', /document\.addEventListener\('click', function\(ev\)\{\n    var t = ev\.target && ev\.target\.closest \? ev\.target\.closest\('\[data-vera-settings\]'\) : null;\n    if\(!t\) return;\n    ev\.preventDefault\(\); ev\.stopPropagation\(\);\n    open\(t\);\n  \}, true\);/.test(VS));
t('blocks are MOVED in and back where they were', /var mark = document\.createComment\('vera-settings'\); k\.parentNode\.insertBefore\(mark, k\);/.test(VS) && /m\.mark\.parentNode\.insertBefore\(m\.el, m\.mark\)/.test(VS) && !/cloneNode/.test(VS));
t('Esc closes, Ctrl/Cmd + , opens', /if\(ev\.key === 'Escape' && _cur\)\{ ev\.preventDefault\(\); close\(\); return; \}/.test(VS) && /\(ev\.ctrlKey \|\| ev\.metaKey\) && ev\.key === ','/.test(VS));
t('an explanation is the line under the title, not a card; only cards with fields or a heading are listed', /lead = _txt\(k\); k\.setAttribute\('data-vs-lead', ''\); return;/.test(VS) && /var nm = cardTitle\(k\); if\(!nm\)\{ rows\.push\(\{ row: null, card: card \}\); return; \}/.test(VS));
t('a section wrapped in one block is read from inside it; a header block is the title and its line', /if\(inner\.cards\.length > 1\) return \{ title: title \|\| inner\.title, lead: lead \|\| inner\.lead, cards: inner\.cards \};/.test(VS) && /if\(!title && !cards\.length && !_fields\(k\) && !_hasButton\(k\)\)\{ var hh = /.test(VS));
t('no // comment swallows code after it on a line', !/[;)}]\s+\/\/[^\n]*\b(if|var|sh|cn)\(/.test(VS) && !/\)\);\s+\/\/[^\n]*;\s*\w/.test(VS));
t('served at /ui/vera-settings.js', /@APP\.get\("\/ui\/vera-settings\.js", include_in_schema=False\)/.test(PY) && /Path\(__file__\)\.parent \/ "vera-settings\.js"/.test(PY));

// the panels on it: their settings item points at a section that exists, and the page loads the script
const PANELS = [['Telegram', ['vera', 'telegram', 'telegram_panel.html']], ['Email', ['vera', 'email', 'email_panel.html']], ['Storage', ['vera', 'proxmox', 'pxstore_panel.html']],
  ['Dream', ['vera', 'dream', 'dream_panel.html']], ['Research', ['vera', 'research', 'research_panel.html']], ['Calendar', ['vera', 'calendar', 'calendar_panel.html']], ['Loop Lab', ['vera', 'evolve', 'evolve_panel.html']]];
for (const [name, file] of PANELS) {
  const H = R(...file);
  const sel = (H.match(/data-vera-settings="([^"]+)"/) || [])[1] || '';
  const exists = sel.startsWith('#') ? H.includes('id="' + sel.slice(1) + '"') : /section\.section\[data-sec='settings'\]/.test(sel) && /<section class="section" data-sec="settings">/.test(H);
  t(name + ': its settings item opens the shared page, on a section that exists', !!sel && exists, sel);
  t(name + ': loads /ui/vera-settings.js', /<script src="\/ui\/vera-settings\.js"><\/script>/.test(H));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
