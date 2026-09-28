// 2026-09-28 (owner): "the layout at the very top of the main dashboard is not polished enough - everything above the operations section"
//   node tests/test_dashboard_top_polish.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'capability_orchestration.html'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the title reads as the page\'s, its subtitle beside it in words', H.includes('#panel-dashboard .dash-toolbar .dash-title{font-size:16px;') && H.includes("l.textContent=(n-secs)+' widgets · '+secs+' sections';"));
t('the arranging buttons are a group of their own', H.includes('#panel-dashboard .dash-toolbar #dashEditBtn::before{'));
t('a section is a banner across its row (no blank band above its heading)', H.includes('.widget.w-section{background:transparent!important;border:none!important;box-shadow:none!important;border-radius:0;justify-content:stretch;') && H.includes('.widget.w-section .w-head{padding:6px 12px 5px;min-height:0;height:100%;align-items:center;'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
