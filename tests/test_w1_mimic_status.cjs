// Wave 1 (panel-derived): the main dashboard's Mimic strip moved once into <vera-mimic-status> (vera/workers/mimic_status_element.js),
// with the queue added so it shows all the desk's key-value reading of cluster.mimic.status did (parity or better). The dashboard
// draws through it; the desk mounts it for a cluster.mimic.status answer, seeded. tests/fixtures/mimic_status_parity.json holds
// what the dashboard's own code drew before.
'use strict';
const fs = require('fs'); const path = require('path'); const vm = require('vm'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const E = R('vera/workers/mimic_status_element.js'), P = R('vera/capability_orchestration.html'), WE = R('vera/widgets/widget_element.js'), CL = R('vera/workers/cluster.py');
const FX = JSON.parse(R('tests/fixtures/mimic_status_parity.json'));
const ctx = { window: {}, HTMLElement: class {}, customElements: { get: () => false, define() {} }, Object, String, Math, Date, JSON }; vm.createContext(ctx); vm.runInContext(E, ctx);
const M = ctx.window.VeraMimicStatus;
const noQueue = FX.cases.map((c, i) => [c, i]).filter(([c]) => !(c.status && (c.status.queue_depth != null || c.status.queue_max != null)));
t('the strip reads as the dashboard drew it (every case without queue fields)', noQueue.every(([c, i]) => M.stripHtml(c.status, c.reqs) === FX.html[i]));
const qi = FX.cases.findIndex((c) => c.status && c.status.queue_depth != null); const qh = M.stripHtml(FX.cases[qi].status, FX.cases[qi].reqs);
t('with the queue: the dashboard strip plus one Queued/max stat after Active/limit, nothing else changed', qh.includes('>3/16</div><div class="cluster-stat-l">Queued/max</div>') && qh.replace(/<div class="cluster-stat" title="[^"]*"><div class="cluster-stat-v" style="color:[^"]*">3\/16<\/div><div class="cluster-stat-l">Queued\/max<\/div><\/div>/, '') === FX.html[qi]);
t('the dashboard draws its strip through the element; its own copy is gone', P.includes("w = document.createElement('vera-mimic-status'); w.setAttribute('refresh', '0');") && P.includes('<script src="/ui/elements/mimic_status.js"></script>') && !P.includes("const stateLbl = !status.mounted ? 'not mounted'"));
t('the element is served and the widget element form knows it', CL.includes('@APP.get("/ui/elements/mimic_status.js", include_in_schema=False)') && WE.includes("'vera-mimic-status': 'mimic_status'"));
const decl = JSON.parse(R('tests/fixtures/presentation_declared.json'))['cluster.mimic.status'] || {};
t('cluster.mimic.status opens as the element (its key-value map kept)', decl.form === 'element' && decl.draw && decl.draw.tag === 'vera-mimic-status' && decl.map && Array.isArray(decl.map.keys));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);
