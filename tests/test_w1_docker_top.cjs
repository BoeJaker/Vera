// Wave 1 (panel-derived): the main dashboard's "Busiest containers" tile (cpu / memory trend per container) moved once into
// <vera-docker-top> (vera/workers/docker_top_element.js). The dashboard draws through it; the desk mounts it for a docker.stats.top
// answer (the widget element form), seeded. tests/fixtures/docker_top_parity.json holds what the dashboard's own code drew before.
'use strict';
const fs = require('fs'); const path = require('path'); const vm = require('vm'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const E = R('vera/workers/docker_top_element.js'), P = R('vera/capability_orchestration.html'), WE = R('vera/widgets/widget_element.js'), DC = R('vera/workers/docker_capabilities.py');
const FX = JSON.parse(R('tests/fixtures/docker_top_parity.json'));
const ctx = { window: {}, HTMLElement: class {}, customElements: { get: () => false, define() {} }, Object, String, Math, Array, JSON }; vm.createContext(ctx); vm.runInContext(E, ctx);
const D = ctx.window.VeraDockerTop;
// the dashboard read its colours with getComputedStyle(...).getPropertyValue; with none set it used the same fallbacks the element passes
const col = (n, fb) => fb;
t('the rows read as the dashboard drew them (three answers: two hosts, none, more than 12)', FX.answers.every((a, i) => (D.rowsHtml(a, 12, col) || '<span style="color:var(--dim2);font-size:9.5px">no container stats yet</span>') === FX.html[i]));
t('the dashboard draws its tile through the element; its own copy is gone', P.includes("w = document.createElement('vera-docker-top'); w.setAttribute('refresh', '0');") && P.includes('<script src="/ui/elements/docker_top.js"></script>') && !P.includes('function _miniSpark(') && !P.includes('rows.slice(0,12).map(c=>{'));
t('the element is served and the widget element form knows it', DC.includes('@APP.get("/ui/elements/docker_top.js", include_in_schema=False)') && WE.includes("'vera-docker-top': 'docker_top'"));
const decl = JSON.parse(R('tests/fixtures/presentation_declared.json'))['docker.stats.top'] || {};
t('docker.stats.top opens as the element; the table stays a view (ranked is not a table-kind view - the contract refuses it)', decl.form === 'element' && decl.draw && decl.draw.tag === 'vera-docker-top' && JSON.stringify(decl.views) === '["table"]');
t('it reads by itself only while seen, and takes an answer it is given (seed)', E.includes("if (!this._every || !this._seen || document.visibilityState === 'hidden') return;") && E.includes('seed(r) { if (r && typeof r === \'object\' && r.hosts)'));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);
