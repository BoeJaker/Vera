// 2026-09-28 (owner): "the context lhm doesnt reliably load in"
// The chat's MENU instance (?only=menu, the harness's LHM slot) was folded at start-up from the STANDALONE chat's rail
// preference (vera_right_collapsed, folded when unset) after _embedMount had opened it, and nothing told the harness -
// so the slot stayed wide and the menu's column sat at opacity 0: the Context menu had loaded and could not be seen.
//   node tests/test_chat_menu_never_folds_unseen.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const restore = C.indexOf("const rc=localStorage.getItem('vera_right_collapsed');");
const fold = C.indexOf("if(railCollapsed){", restore);
const guard = C.indexOf("if(_EMBED.only==='menu') railCollapsed=false;", restore);
t('the start-up restore of the standalone rail preference is found', restore > 0 && fold > restore);
t('the menu instance is never folded by it (the guard sits between the read and the fold)', guard > restore && guard < fold);
t('the menu instance\'s own folds do not write the standalone chat\'s preference',
  /if\(_EMBED\.only!=='menu'\)\{ try\{localStorage\.setItem\('vera_right_collapsed'/.test(C));
t('its folds still reach the harness (vera:lhm:rail)', /if\(_EMBED\.only==='menu'\) window\.parent\.postMessage\(\{ type:'vera:lhm:rail', on:r\.classList\.contains\('slim'\) \}, '\*'\)/.test(C));
t('the harness still opens the menu instance on mount', /if\(_EMBED\.only==='menu'\)\{\n    try\{ const r=document\.getElementById\('rightRail'\); if\(r&&r\.classList\.contains\('slim'\)\) toggleRight\(\); \}catch\(_\)\{\}/.test(C));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
