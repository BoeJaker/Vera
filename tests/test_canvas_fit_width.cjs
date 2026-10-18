// Text-like desk cards are as wide as what they hold and their header needs; a short overflow is a least height, not a clip.
// The owner's desk (2026-10-11, session chat-1791736809311 on the design mirror): a checklist 775 px wide for 285 px of
// text, a note 420 for 51, the "Metric · Value · Change" table 775 for 157 and clipped. Harness: tools/snip_owner_fit.js
// (after: 365, 335, 524 - every title whole - and nothing clipped).   node tests/test_canvas_fit_width.cjs
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const CE = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
t('note, markdown, table, fields and names are measured for how far right their content reaches', CE.includes("if (/^(note|markdown|table|kv|entities)$/.test(c.dataset.type || '')) {") && CE.includes('const nw = this._natW || (this._natW = {});'));
t('the floor is the header: the title\'s TEXT width (a range, not the stretched box) + controls + the header padding the controls sit in', CE.includes('rg.selectNodeContents(tt); tw = Math.ceil(') && CE.includes("(parseFloat(hcs.paddingLeft) || 0) + (parseFloat(hcs.paddingRight) || 0)") && CE.includes("text: (W, m) => (m.used != null ? { w: Math.max(240, m.head, m.used + m.side + 36) } : null)") && CE.includes("this._natural(c.dataset.type, bdw.clientWidth, { used, side: c.offsetWidth - bdw.clientWidth, head }).w"));
t('a card whose content fills its width lets the measure go, so it can widen again', CE.includes('if (used >= bdw.clientWidth - 6) { if (nw[key] != null) { delete nw[key]; changed = true; } }'));
t('placement takes the measured width under the kind\'s own cap - unless the person sized it (WSU-74)', CE.includes("const nwm = user || kind === 'chart' || kind === 'table' ? null : (this._natW || {})[key]; const nw = kind === 'chart' || kind === 'table' ? null : nwm ? Math.min(nw0 || Infinity, nwm) : (user ? null : nw0);"));
t('a short overflow (up to 240 px) becomes the card\'s least height; a long one still scrolls', CE.includes('if (over && nat[key] == null) { const more = bd.scrollHeight - bd.clientHeight; if (more > 4 && more <= 240) {'));
t('minimal tiers: the title has the header while the controls are hidden; the controls come over its end on their own ground', CE.includes(':host(:not([data-tier="full"])) .it:not(.desk-chip) > .it-hd{padding-right:12px!important}') && CE.includes('background:linear-gradient(to right, transparent, var(--bg1,#16181d) 20px)'));
console.log(fails ? fails + ' failed' : 'all passed'); process.exit(fails ? 1 : 0);
