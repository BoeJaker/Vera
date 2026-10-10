// The Vera wordmark stands beside a page's title only when the page is open on its own (owner, 2026-10-10): inside the
// shell, a tile or any frame, the shell's own title says Vera. Live ops no longer carries its own.
//   node tests/test_vera_wordmark_standalone.cjs
const fs = require('fs'), path = require('path');
const R = (...p) => fs.readFileSync(path.join(__dirname, '..', ...p), 'utf8');
const UI = R('vera', 'vera-ui.js'), OPS = R('vera', 'estate', 'ops_panel.html');
let fails = 0; const t = (name, ok) => { console.log((ok ? 'ok   ' : 'FAIL ') + name); if (!ok) fails++; };
t('where the page stands is stamped before the first paint (data-vera-frame standalone | framed)',
  UI.includes("var _standalone = (function(){ try{ return window.self === window.top; }catch(e){ return false; } })();")
  && UI.includes("setAttribute('data-vera-frame', _standalone ? 'standalone' : 'framed')"));
t('standalone only, never in the main shell; beside the menu header, else the top bar title; the tab title says Vera',
  UI.includes('function injectWordmark(){')
  && UI.includes("if(!_standalone || window.__veraNoWordmark || document.querySelector('.vera-wm')) return;")
  && UI.includes("if(document.getElementById('themeBtn')) return;")
  && UI.includes("document.querySelector('#side-head h1') || document.querySelector('header h1, .topbar h1, #topbar h1, .hd h1')")
  && UI.includes("document.title = 'Vera · ' + document.title"));
t('it runs with the rest of the start-up', /injectFloatingPicker\(\);\s*injectWordmark\(\);/.test(UI));
t('Live ops carries no wordmark of its own', !OPS.includes('<div class="wm">VERA</div>'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
