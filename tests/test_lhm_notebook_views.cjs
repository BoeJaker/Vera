// 2026-10-03 (owner), the LHM audit, point 7: "a sub ui is a top bar option that changes the content of the page
// entirely to something else ... all sub uis of a panel should be available from the lhm instead" (top bars are for
// settings and status). Probed on prod: the Notebook's top bar carried ✦ Graph (a full-page graph) and 📄 View (the
// document mode) and the panel docked no menu at all.
//   node tests/test_lhm_notebook_views.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'research', 'notebook_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const top = H.slice(H.indexOf('<div id="topbar">'), H.indexOf('<div id="body">'));
t('the top bar no longer switches the view', top.length > 100 && !/openGraphView\(|enterViewMode\(|nbView\(/.test(top), top.match(/onclick="[^"]*"/g));
t('the top bar keeps its settings, status and actions', /openSettings\(event\)/.test(top) && /syncToGraph\(\)/.test(top) && /exportNotebook\('md'\)/.test(top) && /id="conn-dot"/.test(top));

const nav = (H.match(/<nav id="nb-views" data-vera-lhm[\s\S]*?<\/nav>/) || [''])[0];
t('the views are the panel\'s menu (data-vera-lhm), in its own column', !!nav && H.indexOf(nav) > H.indexOf('<div id="sidebar">'));
const btns = nav.match(/<button class="nav-btn[^"]*"[^>]*>/g) || [];
t('three views: Notes, Document, Graph', btns.length === 3 && ['notes', 'document', 'graph'].every((v) => btns.some((b) => b.includes('data-view="' + v + '"'))));
t('each has a real icon (never a letter)', btns.every((b) => { const m = b.match(/data-icon="([^"]+)"/); return m && !/^[A-Za-z0-9]{1,2}$/.test(m[1]); }));
t('each has a name, a short and a long description', btns.every((b) => /data-label="[^"]+"/.test(b) && /data-desc="[^"]{8,}"/.test(b) && /data-desc-long="[^"]{30,}"/.test(b)));
t('Notes is lit at load', /class="nav-btn active" data-view="notes"/.test(nav));
t('picking a view switches it', /function nbView\(v\)\{/.test(H) && /if\(v==='graph'\)\{[^}]*openGraphView\(\);/.test(H) && /else if\(v==='document'\)\{ closeGraphView\(\); enterViewMode\(\); \}/.test(H));
t('the lit view follows however a view is left (the graph\'s ✕, ← Edit)', /_nbGraph=null; _nbViewMark\(\); \}/.test(H) && /renderVisibleCells\(\);\n  _nbViewMark\(\);\n\}/.test(H));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
