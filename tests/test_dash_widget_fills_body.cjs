// 2026-09-28 (vera-24): on a page's own VeraDash grid a tile's widget kept its natural height - the element tile scrolled inside a
// body with space to spare
//   node tests/test_dash_widget_fills_body.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const VD = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-dashboard.js'), 'utf8');
const WE = fs.readFileSync(path.join(__dirname, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
const ok = VD.includes("'.dash-grid .w-body > vera-widget{flex:1 1 auto;min-height:0;display:block;overflow:hidden}'") && VD.includes("'.dash-grid .widget > .w-head .w-actions{display:none}'") && WE.includes('const bh = Math.max(14, Math.min(38, Math.round((H || 96) - 50)));');
console.log((ok ? 'ok   ' : 'FAIL ') + 'VeraDash stretches a tile\'s widget to its body on every page');
process.exit(ok ? 0 : 1);
