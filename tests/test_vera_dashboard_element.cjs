// 2026-09-27 (owner): "everything should be widgetised and highly polished, and configurable and reusable"
//   node tests/test_vera_dashboard_element.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const VD = R('vera/chat/vera-dashboard.js'), SM = R('vera/monitor/system_monitor_panel.html'), L = JSON.parse(R('vera/widgets/layouts/sysmon.json'));
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('<vera-dashboard> is defined and boots VeraDash on its layout file', /customElements\.define\('vera-dashboard'/.test(VD) && /layout: '\/ui\/widgets\/layouts\/' \+ encodeURIComponent\(layout\)/.test(VD) && /editBtn: editId/.test(VD));
t('it carries its own grid styles, blocks-off included', /vera-dashboard \.dash-grid\{display:grid;grid-template-columns:repeat\(12,minmax\(0,1fr\)\)/.test(VD) && /html\[data-blocks="off"\] vera-dashboard \.widget\{background:transparent/.test(VD));
t('its toolbar: + Widget and Configure (Layouts, Arrange beside it)', /data-a="add"/.test(VD) && /data-a="edit"/.test(VD) && /ctl\.openLoader\(\)/.test(VD));
t('reload() re-reads every tile', /reload\(\) \{ Array\.prototype\.forEach\.call\(this\.querySelectorAll\('vera-widget'\)/.test(VD));
const ids = L.widgets.map((w) => w.record.id);
t('the sysmon layout: the three stacks, the history series, the temperatures', ['sm-proxmox', 'sm-docker', 'sm-ollama', 'sm-cpu-mem', 'sm-proc-ram', 'sm-ollama-load', 'sm-guests', 'sm-queue', 'sm-temp', 'sm-temps'].every((i) => ids.includes(i)));
t('every tile draws a record from a source', L.widgets.every((w) => w.record.form && w.record.source && w.record.draw.body === 'record' && Array.isArray(w.span)));
t('Stack Monitor: Dashboard beside Classic, docked, remembered; Refresh re-reads the tiles', /<vera-dashboard layout="sysmon" no-popout><\/vera-dashboard>/.test(SM) && /<span class="sm-views" data-vera-lhm>/.test(SM) && /localStorage\.setItem\('vera\.sysmon\.view', v\)/.test(SM) && /d\.reload\(\)/.test(SM) && /vera-panel-bridge\.js/.test(SM) && /vera-dashboard\.js/.test(SM));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
