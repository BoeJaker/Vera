// Desk Wave 3: the Loop Lab's commit graph (<vera-git-graph>, vera/git_graph_element.js) draws an evolve.git.graph answer on the desk,
// seeded, with `dated` (author + date, what the desk's table showed). The Loop Lab page sets neither, and draws exactly as before:
// tests/fixtures/git_graph_parity.json is the element's own _render() on the base branch, run in the DOM stub of git_graph_harness.cjs.
'use strict';
const fs = require('fs'); const path = require('path'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const harness = require('./fixtures/git_graph_harness.cjs');
const FX = JSON.parse(R('tests/fixtures/git_graph_parity.json'));
const E = R('vera/git_graph_element.js'), draw = harness(E);
t('without `dated` (the Loop Lab page) every answer draws byte for byte as before', FX.cases.every((c, i) => { const o = draw(c.commits, c.error); return o.rows === FX.out[i].rows && o.count === FX.out[i].count; }));
const d = draw(FX.cases[0].commits, null, { dated: '' });
t('with `dated` each row adds its author and date, nothing else changes',
  d.rows.includes('<span class="when" title="BoeJaker · 2026-10-19T19:48:00+01:00">BoeJaker · 2026-10-19 19:48</span>')
  && d.rows.replace(/<span class="when" title="[^"]*">[^<]*<\/span>/g, '') === FX.out[0].rows);
t('seed() draws a given answer and leaves the page\'s poll and events alone',
  E.includes("seed(x) { if (!x || typeof x !== 'object') return; this._render(x.commits || [], x.error); }") && E.includes('if (this._onScreen()) this.refresh();'));
t('the element form knows it; it is served where the Loop Lab page loads it', R('vera/widgets/widget_element.js').includes("'vera-git-graph': 'git_graph'")
  && R('vera/evolve/evolve_capabilities.py').includes('APP.get("/ui/elements/git_graph.js", include_in_schema=False)('));
const decl = JSON.parse(R('tests/fixtures/presentation_declared.json'))['evolve.git.graph'] || {};
t('evolve.git.graph opens as the graph, dated, for the answer\'s repo (the table kept as its view)', decl.form === 'element' && decl.draw && decl.draw.tag === 'vera-git-graph'
  && decl.draw.attrs && decl.draw.attrs.dated === '1' && decl.draw.attrs.repo === '{repo}' && decl.map && decl.map.rows === 'commits' && Array.isArray(decl.draw.columns));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);
