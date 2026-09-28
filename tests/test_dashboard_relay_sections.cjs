// 2026-09-28 (owner): the default layout "could have clearer sections" · "condense any repeated widgets" · the big-text
// tiles need "an extra graphic" · (vera-24) a narrow tile's title was squeezed out by the record chip
//   node tests/test_dashboard_relay_sections.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const M = JSON.parse(R('vera/widgets/layouts/main.json')), VD = R('vera/chat/vera-dashboard.js'), CH = R('vera/capability_orchestration.html');
let fails = 0; const t = (name, cond, x) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (x || ''))); if (!cond) fails++; };
const id = (k) => M.widgets.find((w) => w.record && w.record.id === k);
const shown = M.widgets.filter((w) => !w.hidden && Array.isArray(w.at));
const secs = shown.filter((w) => w.record.form === 'section');
t('every section has a colour of its own and a subtitle after its name', secs.length >= 9 && secs.every((w) => /^var\(--b-dv[1-7],#[0-9a-f]{6}\)$/.test(w.record.draw.accent) && / · /.test(w.record.title)), secs.map((w) => w.record.id).join(' '));
t('the repeats are folded away (each said what another tile says)', ['requests', 'workerlist', 'agent-programmes', 'cap-stream', 'fleet', 'guests', 'routing', 'fabric'].every((k) => id(k).hidden));
const sorted = shown.every((w, i) => !i || shown[i - 1].at[1] < w.at[1] || (shown[i - 1].at[1] === w.at[1] && shown[i - 1].at[0] < w.at[0]));
t('the file is in row-major order (VeraDash lays tiles in file order) and no tile is taller than six rows', sorted && shown.every((w) => w.span[1] <= 6));
t('memory stores draws what each holds (a bar a store), not three big figures', id('memory-stores').record.form === 'ranked' && Object.keys(id('memory-stores').record.read.map.pick).length === 4 && !id('memory-stores').record.children);
const WE = R('vera/widgets/widget_element.js');
t('a ranked figure is said short from ten thousand (356.1k, 4.33M) so it fits its column', WE.includes("a >= 1e6 ? f(n / 1e6, 'M', 100) : a >= 1e4 ? f(n / 1e3, 'k', 10) : fmt(v); };") && WE.includes('r.size ?? rkShort(x[1])'));
t('fabric health and worldview are four figures each, not a list of seven keys', id('fabric-health').record.form === 'numbers' && id('worldview').record.form === 'numbers' && Object.keys(id('worldview').record.read.map.pick).length === 4);
t('the section head: the name bold, the rest a subtitle, its own colour', VD.includes("secT.innerHTML = '<b>' + esc(secS.slice(0, secCut)) + '</b><small>' + esc(secS.slice(secCut)) + '</small>';") && VD.includes("widget.style.setProperty('--sec-acc', String(record.draw.accent));") && CH.includes(".widget.w-section .w-title::before{content:'';") && CH.includes('background:var(--sec-acc,var(--acc,#5a9e8f))}'));
t('on a page\'s own grid the record chip folds away until hover, arranging or news, so a narrow title stays', VD.includes("'.dash-grid .w-head .vd-rec{max-width:0;opacity:0;margin-left:0;") && VD.includes("'.dash-grid .w-head .w-title{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
