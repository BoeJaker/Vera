// Wave 1 (panel-derived): the certs panel's render() (findings; every certificate with where, issuer, covers, expiry, days left,
// state) moved once into <vera-certs> (vera/security/certs_element.js). The panel draws through it and keeps its two controls
// (show superseded, Check now); the desk mounts it for a certs.list answer, seeded, with `counts` (the by-state row the former
// composite showed). tests/fixtures/certs_parity.json holds what the panel's own code drew before.
'use strict';
const fs = require('fs'); const path = require('path'); const vm = require('vm'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const E = R('vera/security/certs_element.js'), P = R('vera/security/certs_panel.html'), WE = R('vera/widgets/widget_element.js'), CC = R('vera/security/certs_capabilities.py');
const FX = JSON.parse(R('tests/fixtures/certs_parity.json'));
class D extends Date { toLocaleString() { return this.toISOString(); } }
const ctx = { window: {}, HTMLElement: class {}, customElements: { get: () => false, define() {} }, Date: D, Object, String, Math, JSON }; vm.createContext(ctx); vm.runInContext(E, ctx);
const M = ctx.window.VeraCerts;
t('the findings read as the panel drew them, byte for byte (every case)', FX.cases.every((c, i) => M.findingsHtml(c.data) === FX.out[i].findings));
t('every certificate reads as the panel drew it, with and without superseded (every case)', FX.cases.every((c, i) => M.certsHtml(c.data, c.showOld) === FX.out[i].certs));
t('the checked line reads as the panel wrote it (every case)', FX.cases.every((c, i) => M.metaText(c.data) === FX.out[i].meta));
const cn = M.countsHtml(FX.cases[0].data);
t('counts: one pill per state with certificates, in the state pills\' colours; none when there are no counts',
  cn.includes('<span class="pill warn">renew soon 1</span>') && cn.includes('<span class="pill ok">ok 1</span>') && cn.includes('<span class="pill ">superseded 1</span>') && M.countsHtml(FX.cases[1].data) === '');
t('the panel draws through the element and keeps its controls; its own render copy is gone',
  P.includes('<vera-certs id="certs"></vera-certs>') && P.includes('<script src="/ui/elements/certs.js"></script>')
  && P.includes("function render(){ $('certs').showOld = $('showOld').checked; }") && P.includes("function load(refresh){ $('certs').load(!!refresh); }")
  && P.includes('id="showOld" onchange="render()"') && P.includes('onclick="load(true)"') && !P.includes('const STATE_KIND') && !P.includes('<th>Covers</th>'));
t('the element is served and the widget element form knows it', CC.includes('@APP.get("/ui/elements/certs.js", include_in_schema=False)') && WE.includes("'vera-certs': 'certs'"));
const decl = JSON.parse(R('tests/fixtures/presentation_declared.json'))['certs.list'] || {};
t('certs.list opens as the element with counts (the certificate table kept as its view, certificates as estate refs)', decl.form === 'element' && decl.draw && decl.draw.tag === 'vera-certs'
  && decl.draw.attrs && decl.draw.attrs.counts === '1' && decl.map && decl.map.rows === 'certs' && decl.draw.ref === 'cert:{name}');
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);
