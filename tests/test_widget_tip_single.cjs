// 2026-09-27: a block's hover card - one per widget, hidden when the pointer leaves (':scope' matches nothing in a shadow root)
//   node tests/test_widget_tip_single.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const W = fs.readFileSync(path.join(__dirname, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('no :scope lookups inside the shadow root', !/querySelector\(':scope > \.vw-tip'\)/.test(W));
t('the card is kept on the root and reused', /let t = root\._vwTip; if \(!t \|\| !t\.isConnected\)/.test(W) && /root\._vwTip = t;/.test(W));
t('leaving hides it', /const t = root\._vwTip; if \(t\) t\.hidden = true;/.test(W));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
