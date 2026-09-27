// 2026-09-27 (owner): "the blocks view mode system should be transferred to all other ui panels"
//   node tests/test_blocks_by_role.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const D = fs.readFileSync(path.join(__dirname, '..', 'vera', 'ui', 'design.css'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('blocks off: surfaces by role go see-through, firmly (ids outrank classes)', /html:root\[data-blocks="off"\] body :is\(aside, nav, header, footer,/.test(D) && /\[id\*="sidebar"\]/.test(D) && /\[class\*="toolbar"\]/.test(D) && /background: transparent !important; box-shadow: none !important;/.test(D));
t('what must stay readable keeps its ground', /:not\(:is\(\[class\*="pop"\], \[id\*="pop"\], \[class\*="modal"\]/.test(D) && /pre, code, input, select, textarea, button, \.btn,/.test(D));
t('blocks on: cards and tiles are raised', /html:root:not\(\[data-blocks="off"\]\) body :is\(\[class\$="-card"\], \[class\$="-tile"\]/.test(D));
t('meters and bars that are data keep their fill', /\[class\*="progress"\], \[id\*="progress"\], \[class\*="meter"\], \[class\*="fill"\]/.test(D));
t('the :not() is joined to the :is() (a space would make it a descendant rule)', !/\)\s*\n\s*:not\(:is\(/.test(D) && /\):not\(:is\(\[class\*="pop"\], \[id\*="pop"\]/.test(D));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
