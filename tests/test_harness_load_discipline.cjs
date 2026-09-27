// 2026-09-27 (the widget review): the harness starved its own connections - 20 hidden panel frames loading at boot, polls
// stacking while pending, hidden loaders drawn at 60 fps
//   node tests/test_harness_load_discipline.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const HAR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'capability_orchestration.html'), 'utf8');
const OV = fs.readFileSync(path.join(__dirname, '..', 'vera', 'activity_overlay.js'), 'utf8');
const LD = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera-loader.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

t('a frame put into a panel that is not showing is parked', /function _framePark\(f\)\{/.test(HAR) && /f\.setAttribute\('data-lazy-src', src\); f\.setAttribute\('data-lazy', '1'\); f\.setAttribute\('src', 'about:blank'\);/.test(HAR));
t('...not one in the open panel or an open tab', /if\(p\.classList\.contains\('active'\) \|\| _openTabs\.has\(_panelIdFromEl\(p\)\)\) return;/.test(HAR));
t('watching the panels from the start', /root\.querySelectorAll\('iframe'\)\.forEach\(_framePark\);/.test(HAR));
t('the Elements tab loads each page when its sub-tab is shown', /_deferPanelIframes\(cont\);   \/\/ parked while detached/.test(HAR) && /_mediaLoad\(document\.getElementById\(\`mc_\$\{p\.id\}\`\)\);/.test(HAR) && /function _mediaLoad\(cont\)\{/.test(HAR));
t('the panel-set read never overlaps itself, nor runs hidden', /if\(_panelsSetBusy \|\| document\.hidden\) return; _panelsSetBusy = true;/.test(HAR));
t('the activity timeline too', /if \(polling \|\| document\.hidden\) return; polling = true;/.test(OV));
t('a loader laid out nowhere is not drawn', /if \(!f\.canvas\.offsetWidth && !f\.canvas\.offsetHeight\) continue;/.test(LD));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
