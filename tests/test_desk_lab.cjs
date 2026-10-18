// The Desk Lab's pure parts (framework F3): URL parsing, the budget, percentiles, scenario steps, and how a state is
// made without the network.  node tests/test_desk_lab.cjs
const path = require('node:path'), fs = require('node:fs');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = path.join(__dirname, '..');
const L = require(path.join(R, 'vera', 'ui', 'desk_lab.js'));
const known = ['stepper', 'pipeline', 'heat', 'log', 'lane', 'table', 'heat@iso', 'trace', 'area'];

let q = L.parseQuery('?forms=stepper,heat,nope&sizes=s,m,zz&states=live,failed,bogus&theme=light&views=1&scenario=sweep', known);
t('forms keep known names only, in order', JSON.stringify(q.forms) === '["stepper","heat"]', JSON.stringify(q.forms));
t('sizes and states keep the valid ones', JSON.stringify(q.sizes) === '["s","m"]' && JSON.stringify(q.states) === '["live","failed"]');
t('theme, views and scenario are read', q.theme === 'light' && q.views === true && q.scenario === 'sweep' && q.data === 'sample');
q = L.parseQuery('?forms=stages', known);
t('a family word expands to its forms that exist', JSON.stringify(q.forms) === '["stepper","pipeline"]', JSON.stringify(q.forms));
q = L.parseQuery('', known);
t('defaults: stepper, s/m/l, live, dark, no scenario', JSON.stringify(q) === JSON.stringify({ forms: ['stepper'], sizes: ['s', 'm', 'l'], states: ['live'], theme: 'dark', views: false, data: 'sample', scenario: 'none' }), JSON.stringify(q));
t('"all" takes every known form', L.parseQuery('?forms=all', known).forms.length === known.length);
const back = L.parseQuery(L.toQuery({ forms: ['heat', 'log'], sizes: ['xl'], states: ['stale'], theme: 'light', views: true, data: 'json', scenario: 'sweep' }), known);
t('toQuery and parseQuery round-trip', back.forms.join() === 'heat,log' && back.sizes.join() === 'xl' && back.states.join() === 'stale' && back.theme === 'light' && back.views && back.data === 'json' && back.scenario === 'sweep');

t('percentile: p50 and p95 of a sample', L.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) === 5 && L.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) === 10 && L.percentile([], 95) === 0);
t('over budget: a slow card draw, a huge DOM, an error', L.overBudget({ size: 'm', drawMs: 60, dom: 10 }).length === 1 && L.overBudget({ size: 'l', drawMs: 60, dom: 10 }).length === 0
  && L.overBudget({ size: 'm', drawMs: 1, dom: 2000 }).length === 1 && L.overBudget({ size: 's', drawMs: 1, dom: 1, error: 'x' }).length === 1);
const s = L.summarise([{ size: 'm', drawMs: 2, dom: 100 }, { size: 'm', drawMs: 80, dom: 50 }], [10, 12, 30]);
t('summary: counts over budget, draw percentiles, DOM max, frame p95 and its verdict', s.cells === 2 && s.over === 1 && s.domMax === 100 && s.frameP95 === 30 && s.frameOver === true);
t('the sweep visits every cell; other scenarios have no steps', L.scenarioSteps('sweep', ['a', 'b']).length === 2 && L.scenarioSteps('none', ['a']).length === 0);

t('states without the network: sample has no data, empty has [], reading has a source, live/stale carry the fixture',
  L.stateRecord('heat', 'sample', { a: 1 }, 'm').data === undefined && Array.isArray(L.stateRecord('heat', 'empty', { a: 1 }, 'm').data)
  && L.stateRecord('heat', 'reading', { a: 1 }, 'm').source === 'desklab.status' && L.stateRecord('heat', 'live', { a: 1 }, 'm').data.a === 1 && L.stateRecord('heat', 'stale', { a: 1 }, 'm').data.a === 1);
const el = {}; L.applyState(el, 'failed'); const el2 = {}; L.applyState(el2, 'stale'); const el3 = {}; L.applyState(el3, 'reading'); const el4 = {}; L.applyState(el4, 'empty');
t('applyState sets the fields the widget frame reads (err, read, a call that never answers)', el._err && el._read === false && el2._err && el2._read === true && typeof el3._call === 'function' && el4._read === true);

const LIBS = fs.readFileSync(path.join(R, 'vera', 'ui', 'libs.py'), 'utf8');
const HTML = fs.readFileSync(path.join(R, 'vera', 'ui', 'desk_lab.html'), 'utf8');
t('served at /ui/desk-lab and /ui/desk-lab.js', LIBS.includes('@APP.get("/ui/desk-lab", include_in_schema=False)') && LIBS.includes('@APP.get("/ui/desk-lab.js", include_in_schema=False)'));
t('the page loads the shared parts in order (bootstrap, vera-ui, iso, widgets, the lab)', /bootstrap\.css[\s\S]*vera-ui\.js[\s\S]*iso\.js[\s\S]*widget_element\.js[\s\S]*desk-lab\.js/.test(HTML));
t('the page colours come from theme names with fallbacks (no bare hex outside var())', !/[:\s]#[0-9a-f]{3,6}\b(?![^(]*\))/i.test(HTML.replace(/var\([^)]*\)/g, '')));
t('the widget element exports viewsFor (the lab draws view-as variants)', /window\.VeraWidget = \{ draw, forms, viewsFor,/.test(fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_element.js'), 'utf8')));
console.log(fails ? fails + ' failed' : 'all passed'); process.exit(fails ? 1 : 0);
