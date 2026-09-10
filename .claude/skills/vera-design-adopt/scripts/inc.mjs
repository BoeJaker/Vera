// Embed (or refresh) the shared iso library in each artboard named on the
// command line. The library lives in _lib/isolib.js; each board carries a copy
// between @iso-lib markers, replaced wholesale on every run so they can never
// drift. A board without the markers gets them inserted at the top of its
// script.
import fs from 'node:fs';
import path from 'node:path';
const here = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
const lib = fs.readFileSync(path.join(here, '_lib', 'isolib.js'), 'utf8').replace(/\r\n/g, '\n').trim();
const OPEN = '/*@iso-lib*/', CLOSE = '/*@/iso-lib*/';
for (const f of process.argv.slice(2)){
  let t = fs.readFileSync(f, 'utf8').replace(/\r\n/g, '\n');
  const a = t.indexOf(OPEN), b = t.indexOf(CLOSE);
  if (a >= 0 && b > a){
    t = t.slice(0, a) + lib + t.slice(b + CLOSE.length);
  } else {
    const i = t.indexOf('data-dc-script');
    if (i < 0) throw new Error('no dc script in ' + f);
    const j = t.indexOf('>', i) + 1;
    t = t.slice(0, j) + '\n' + lib + '\n' + t.slice(j);
  }
  fs.writeFileSync(f, t);
  console.log('iso-lib -> ' + path.basename(f));
}
