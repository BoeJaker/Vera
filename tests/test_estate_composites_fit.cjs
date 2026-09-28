// 2026-09-28 (owner): the Docker, Proxmox and Ollama composites were "getting their content cut off at the bottom"
//   node tests/test_estate_composites_fit.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const M = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'vera', 'widgets', 'layouts', 'main.json'), 'utf8'));
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
const kids = (id) => M.widgets.find((w) => w.record && w.record.id === id).record.children.map((c) => c.record);
t('their four-key lists are figures (Proxmox two, its slot is narrow)', ['sysmon-docker', 'sysmon-proxmox'].every((id) => kids(id)[0].form === 'numbers') && Object.keys(kids('sysmon-docker')[0].read.map.pick).length === 4 && Object.keys(kids('sysmon-proxmox')[0].read.map.pick).length === 2);
t('their lists are three rows (the drawer has the rest)', kids('sysmon-docker')[1].draw.limit === 3 && kids('sysmon-proxmox')[2].draw.limit === 3 && kids('sysmon-ollama').slice(1).every((c) => c.draw.limit === 3));
const CH = fs.readFileSync(path.join(__dirname, '..', 'vera', 'capability_orchestration.html'), 'utf8');
t('the old cards no longer cap the body the record tile draws into (290 px of a six-row tile)', !CH.includes('gap:6px;max-height:290px;overflow-y:auto') && (CH.match(/gap:6px;min-height:0;overflow-y:auto/g) || []).length === 3);
const WE = fs.readFileSync(path.join(__dirname, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
t('a bare tile measures its root (the box the content has), not the element', WE.includes("(this.hasAttribute('bare') ? (this._sh.querySelector('.vw-root') || this) : null); const hb = b ? b.clientHeight : 0"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
