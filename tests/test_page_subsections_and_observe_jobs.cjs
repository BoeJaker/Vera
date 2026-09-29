// 2026-09-28 (owner): "the estate ui's observe menu is missing the perf section and the observe page could also let you monitor
// vera jobs and llm jobs on the front page"
//   node tests/test_page_subsections_and_observe_jobs.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const BR = R('vera/chat/vera-panel-bridge.js'), LHM = R('vera/chat/vera-lhm.js'), WO = R('vera/workers/workers_ollama_panel.html'), O = JSON.parse(R('vera/widgets/layouts/wol-observe.json'));
let fails = 0; const t = (name, cond, x) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (x || ''))); if (!cond) fails++; };
t('a page\'s own sub-sections (data-vera-sub) are listed under the open item, a framed child\'s one level deeper', BR.includes('function _pageSubs(){') && BR.includes("sub.push({ id: 's:' + it.id, label: it.label, depth: 1 });") && BR.includes("depth: ps ? 2 : 1"));
t('a pick of one clicks its tab, and a switch re-publishes the menu', BR.includes("if(id.indexOf('s:') === 0){ var ps = _pageSubs()") && BR.includes("hit.el.click();") && BR.includes("(ps ? ps.items.map(function(it){ return it.id; }).join(',') + '>' + ps.active : '')"));
t('a strip in a bar the harness absorbed still counts as shown', BR.includes("if(cs.display === 'none' && !n.hasAttribute('data-vpb-hdr-bar')) return false;"));
const VP = R('vera/vera-panel.js');
t('the panel\'s own menu mirrors the shown strip under the lit item, a pick clicks the tab', VP.includes("document.querySelectorAll('[data-vera-sub]'), subShown)") && VP.includes("b.addEventListener('click', function () { t.click();") && VP.includes("after.parentNode.insertBefore(b, after.nextSibling)"));
t('the menu draws a second level', LHM.includes("(t.depth > 1 ? ' sub2' : '')") && LHM.includes('.lhm-absorbed .lhm-tab.sub2{'));
t('the Estate\'s sub-tab strips name their sections - Observe\'s Events and Perf among them', (WO.match(/data-vera-sub="/g) || []).length === 19 && WO.includes("onclick=\"P.obsSub('perf')\" data-vera-sub=\"perf\"") && WO.includes("onclick=\"P.obsSub('events')\" data-vera-sub=\"events\""));
const ids = O.widgets.map((w) => w.record && w.record.id);
/* 2026-09-28 (owner): "the widgets in observe panel are not focused on errors and they must be" - the warnings and errors
   lead now; the jobs - Vera's and the LLM's - follow them, still above the event stream and the log */
const ERRS = ['obs-errors', 'obs-warnings', 'obs-last-error', 'obs-error-trend', 'obs-error-log', 'obs-errors-by-cap'];
t('the Observe front page opens on the errors, then the jobs - Vera\'s and the LLM\'s - above the event stream and the log', ERRS.every((id, i) => ids[i] === id) && ['obs-inflight', 'obs-gpu-gate', 'obs-llm-by-worker', 'obs-llm-rate', 'obs-vera-work', 'obs-llm-routing', 'obs-job-stream'].every((id, i) => ids[i + ERRS.length] === id) && ids.indexOf('obs-stream') > 12);
const cells = {}; let overlap = '', rows = 0; O.widgets.filter((w) => !w.hidden && Array.isArray(w.at)).forEach((w) => { for (let i = w.at[0]; i < w.at[0] + w.span[0]; i++) for (let j = w.at[1]; j < w.at[1] + w.span[1]; j++) { const k = i + ',' + j; if (cells[k]) overlap = overlap || w.record.id; cells[k] = 1; rows = Math.max(rows, j + 1); } });
const holes = []; for (let j = 0; j < rows; j++) for (let i = 0; i < 12; i++) if (!cells[i + ',' + j]) holes.push(i + ',' + j);
// the tallest a tile may be is VeraDash's MAX_ROWS (six until 2026-09-28)
const MAXR = +((require('node:fs').readFileSync(require('node:path').join(__dirname, '..', 'vera', 'chat', 'vera-dashboard.js'), 'utf8').match(/var MAX_ROWS = (\d+);/) || [])[1] || 6);
t('no overlap, no holes, nothing taller than VeraDash allows', !overlap && !holes.length && O.widgets.every((w) => !w.span || w.span[1] <= MAXR), overlap + ' ' + holes.slice(0, 4).join(' ') + ' MAX_ROWS ' + MAXR);
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
