// Intel panel: values inside inline onclick handlers (review 2026-10-10, intel D9). esc() alone was undone by the HTML
// parser before the JS ran, so "Moody's" broke a Watch button and crafted listing data could inject script.
//   node tests/test_intel_panel_escaping.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const P = fs.readFileSync(path.join(__dirname, '..', 'vera', 'intel', 'intel_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const grab = (re) => { const m = P.match(re); if (!m) { t('found ' + re, false); process.exit(1); } return m[0]; };
const api = new Function(grab(/const esc = \(s\) => .*;/) + '\n' + grab(/const jsq = \(s\) => .*;/) + '\nreturn {esc, jsq};')();

// what a browser does with the attribute: decode the entities, then run the handler text as JS
const decode = (a) => a.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const hostile = ["Moody's", 'say "hi"', "x');alert(1);('", 'back\\slash', '</button><script>alert(1)</script>', 'line\nbreak'];
for (const v of hostile) {
  const html = `<button onclick="priceWatch(${api.jsq('yahoo')},${api.jsq(v)},this)">Watch</button>`;
  const attr = html.match(/onclick="([^"]*)"/);
  t('the attribute stays one attribute: ' + JSON.stringify(v), !!attr && !/<script/i.test(attr[1]));
  let got = null;
  new Function('priceWatch', 'this_', decode(attr[1]).replace(/,this\)$/, ',this_)'))((a, b) => { got = [a, b]; }, null);
  t('the handler receives the exact value: ' + JSON.stringify(v), got && got[0] === 'yahoo' && got[1] === v, JSON.stringify(got));
}
t("no inline handler interpolates esc() inside quotes any more", !/onclick="[^"]*'\$\{esc\(/.test(P));
t('the Watch buttons use jsq', /onclick="priceWatch\(\$\{jsq\(p\.provider\)\},\$\{jsq\(p\.symbol\)\},\$\{jsq\(p\.name\)\},this\)"/.test(P));

if (fails) { console.log(fails + ' failed'); process.exit(1); }
console.log('all passed');
